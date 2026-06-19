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
  updateMe: (data: { display_name?: string; assistant_name?: string }) =>
    api.patch('/users/me', data),
}

export const conversationsApi = {
  getAll: (includeDeleted?: boolean, includeUnprocessed?: boolean, limit?: number, offset?: number, starredOnly?: boolean, sortBy?: string, sortOrder?: string) => api.get('/api/conversations', {
    params: {
      ...(includeDeleted !== undefined && { include_deleted: includeDeleted }),
      ...(includeUnprocessed !== undefined && { include_unprocessed: includeUnprocessed }),
      ...(starredOnly !== undefined && { starred_only: starredOnly }),
      ...(limit !== undefined && { limit }),
      ...(offset !== undefined && { offset }),
      ...(sortBy !== undefined && { sort_by: sortBy }),
      ...(sortOrder !== undefined && { sort_order: sortOrder }),
    }
  }),
  getById: (id: string) => api.get(`/api/conversations/${id}`),
  search: (query: string, limit?: number, offset?: number) =>
    api.get('/api/conversations/search', { params: { q: query, limit, offset } }),
  star: (id: string) => api.post(`/api/conversations/${id}/star`),
  delete: (id: string) => api.delete(`/api/conversations/${id}`),
  restore: (id: string) => api.post(`/api/conversations/${id}/restore`),
  permanentDelete: (id: string) => api.delete(`/api/conversations/${id}`, {
    params: { permanent: true }
  }),

  // Reprocessing endpoints
  reprocessOrphan: (conversationId: string) => api.post(`/api/conversations/${conversationId}/reprocess-orphan`),
  reprocessTranscript: (conversationId: string) => api.post(`/api/conversations/${conversationId}/reprocess-transcript`),
  reprocessMemory: (conversationId: string, transcriptVersionId: string = 'active') => api.post(`/api/conversations/${conversationId}/reprocess-memory`, null, {
    params: { transcript_version_id: transcriptVersionId }
  }),
  reprocessSpeakers: (
    conversationId: string,
    transcriptVersionId: string = 'active'
  ) =>
    api.post(`/api/conversations/${conversationId}/reprocess-speakers`, null, {
      params: {
        transcript_version_id: transcriptVersionId
      }
    }),

  // Version management (transcript only — memory is no longer versioned)
  activateTranscriptVersion: (conversationId: string, versionId: string) => api.post(`/api/conversations/${conversationId}/activate-transcript/${versionId}`),
  getVersionHistory: (conversationId: string) => api.get(`/api/conversations/${conversationId}/versions`),

  // Memory vault change history (audit ledger)
  getMemoryAudit: (conversationId: string, limit: number = 100) => api.get(`/api/conversations/${conversationId}/memory-audit`, { params: { limit } }),

  // Active conversation management
  closeActiveConversation: (clientId: string) => api.post(`/api/conversations/${clientId}/close`),
}

// One recorded change to the memory vault (the audit ledger). Content lives in
// the per-entry diff endpoint, not the list, so the list stays light.
export interface MemoryAuditEntry {
  id: string
  user_id: string
  conversation_id: string | null
  operation: 'create' | 'update' | 'delete' | 'rename' | 'delete_all'
  note_path: string | null
  // Provenance: `cause` is why the memory changed, `strategy` is how the vault
  // was updated (control flow). `source_kind`/`source_label`/`actor` are the
  // backend-classified taxonomy the UI renders directly.
  cause: string | null
  strategy: string | null
  source_kind: 'extraction' | 'reprocess' | 'human' | 'agent' | 'bulk' | 'other'
  source_label: string
  actor: 'system' | 'user' | 'human_external' | 'agent'
  provider: string
  agent_mode: boolean
  before_hash: string | null
  after_hash: string | null
  after_bytes: number | null
  summary: string | null
  extra: Record<string, unknown>
  created_at: string | null
  has_diff: boolean
}

export interface MemoryAuditDiff {
  id: string
  note_path: string | null
  operation: string
  cause: string | null
  created_at: string | null
  before_text: string | null
  after_text: string | null
  diff: string
  diff_available: boolean
  reason?: string
}

export const memoryApi = {
  // Memory vault change ledger (newest first). `user_id` honored for admins only.
  getAudit: (params?: { limit?: number; conversation_id?: string; user_id?: string }) =>
    api.get<{ user_id: string; count: number; entries: MemoryAuditEntry[] }>(
      '/api/memories/audit',
      { params }
    ),
  // Lazily-fetched before→after diff for one ledger entry.
  getAuditDiff: (entryId: string) =>
    api.get<MemoryAuditDiff>(`/api/memories/audit/${entryId}/diff`),
}

export const annotationsApi = {
  // Create annotations
  createMemoryAnnotation: (data: {
    memory_id: string
    original_text: string
    corrected_text: string
  }) => api.post('/api/annotations/memory', data),

  createTranscriptAnnotation: (data: {
    conversation_id: string
    segment_index: number
    original_text: string
    corrected_text: string
  }) => api.post('/api/annotations/transcript', data),

  // Retrieve annotations
  getMemoryAnnotations: (memory_id: string) =>
    api.get(`/api/annotations/memory/${memory_id}`),

  getTranscriptAnnotations: (conversation_id: string) =>
    api.get(`/api/annotations/transcript/${conversation_id}`),

  // Handle suggestions
  acceptSuggestion: (annotation_id: string) =>
    api.patch(`/api/annotations/${annotation_id}/status`, { status: 'accepted' }),

  rejectSuggestion: (annotation_id: string) =>
    api.patch(`/api/annotations/${annotation_id}/status`, { status: 'rejected' }),

  // Diarization annotations
  createDiarizationAnnotation: (data: {
    conversation_id: string
    segment_index: number
    original_speaker: string
    corrected_speaker: string
    segment_start_time?: number
  }) => api.post('/api/annotations/diarization', data),

  getDiarizationAnnotations: (conversation_id: string) =>
    api.get(`/api/annotations/diarization/${conversation_id}`),

  // Apply diarization annotations (creates new version)
  applyDiarizationAnnotations: (conversation_id: string) =>
    api.post(`/api/annotations/diarization/${conversation_id}/apply`),

  // Apply ALL pending annotations (diarization + transcript + insert) - creates single new version
  applyAllAnnotations: (conversation_id: string) =>
    api.post(`/api/annotations/${conversation_id}/apply`),

  // Title annotations (instantly applied)
  createTitleAnnotation: (data: {
    conversation_id: string
    original_text: string
    corrected_text: string
  }) => api.post('/api/annotations/title', data),

  getTitleAnnotations: (conversation_id: string) =>
    api.get(`/api/annotations/title/${conversation_id}`),

  // Generic annotation management
  deleteAnnotation: (annotationId: string) =>
    api.delete(`/api/annotations/${annotationId}`),

  updateAnnotation: (annotationId: string, data: {
    corrected_text?: string
    corrected_speaker?: string
    insert_text?: string
    insert_segment_type?: string
    insert_speaker?: string
  }) => api.patch(`/api/annotations/${annotationId}`, data),

  // Insert annotations
  createInsertAnnotation: (data: {
    conversation_id: string
    insert_after_index: number
    insert_text: string
    insert_segment_type: string
    insert_speaker?: string
  }) => api.post('/api/annotations/insert', data),

  getInsertAnnotations: (conversation_id: string) =>
    api.get(`/api/annotations/insert/${conversation_id}`),
}

export const finetuningApi = {
  // Process annotations for training
  processAnnotations: (annotationType: string = 'diarization') =>
    api.post('/api/finetuning/process-annotations', null, {
      params: { annotation_type: annotationType }
    }),

  // Get fine-tuning status
  getStatus: () => api.get('/api/finetuning/status'),

  // Orphaned annotation management
  deleteOrphanedAnnotations: (annotationType?: string) =>
    api.delete('/api/finetuning/orphaned-annotations', {
      params: annotationType ? { annotation_type: annotationType } : {}
    }),
  reattachOrphanedAnnotations: () =>
    api.post('/api/finetuning/orphaned-annotations/reattach'),

  // Cron job management
  getCronJobs: () => api.get('/api/finetuning/cron-jobs'),
  updateCronJob: (jobId: string, data: { enabled?: boolean; schedule?: string }) =>
    api.put(`/api/finetuning/cron-jobs/${jobId}`, data),
  runCronJob: (jobId: string) =>
    api.post(`/api/finetuning/cron-jobs/${jobId}/run`),
}

export const usersApi = {
  getAll: () => api.get('/api/users'),
  create: (userData: any) => api.post('/api/users', userData),
  update: (id: string, userData: any) => api.put(`/api/users/${id}`, userData),
  delete: (id: string) => api.delete(`/api/users/${id}`),
}

export const clientsApi = {
  list: () => api.get('/api/clients'),
  rename: (clientId: string, name: string) => api.patch(`/api/clients/${clientId}`, { name }),
  forget: (clientId: string) => api.delete(`/api/clients/${clientId}`),
}

export const systemApi = {
  getHealth: () => api.get('/health'),
  getReadiness: () => api.get('/readiness'),
  getMetrics: () => api.get('/api/metrics'),
  getConfigDiagnostics: () => api.get('/api/config/diagnostics'),
  getProcessorStatus: () => api.get('/api/processor/status'),
  getProcessorTasks: () => api.get('/api/processor/tasks'),
  getActiveClients: () => api.get('/api/clients/active'),
  getDiarizationSettings: () => api.get('/api/diarization-settings'),
  saveDiarizationSettings: (settings: any) => api.post('/api/diarization-settings', settings),

  // ASR hint mechanism (keyword boosting vs LLM context prompt) + per-provider context
  getAsrContext: () => api.get('/api/asr-context'),
  saveAsrContext: (model_name: string, context: string) =>
    api.post('/api/asr-context', { model_name, context }),

  // Miscellaneous Configuration Settings
  getMiscSettings: () => api.get('/api/misc-settings'),
  saveMiscSettings: (settings: {
    always_persist_enabled?: boolean;
    per_segment_speaker_id?: boolean;
    streaming_fallback_timeout_seconds?: number;
    always_batch_retranscribe?: boolean;
    live_segmentation?: 'streaming_stt' | 'windowed_batch' | 'off';
  }) => api.post('/api/misc-settings', settings),

  // Plugin Configuration Management (YAML-based)
  getPluginsConfigRaw: () => api.get('/api/admin/plugins/config'),
  updatePluginsConfigRaw: (configYaml: string) =>
    api.post('/api/admin/plugins/config', configYaml, {
      headers: { 'Content-Type': 'text/plain' }
    }),
  validatePluginsConfig: (configYaml: string) =>
    api.post('/api/admin/plugins/config/validate', configYaml, {
      headers: { 'Content-Type': 'text/plain' }
    }),

  // Plugin Configuration Management (Structured/Form-based)
  getPluginsMetadata: () => api.get('/api/admin/plugins/metadata'),
  updatePluginConfigStructured: (pluginId: string, config: {
    orchestration?: {
      enabled: boolean
      events: string[]
      condition: { type: string; wake_words?: string[]; keywords?: string[]; threshold?: number }
    }
    settings?: Record<string, any>
    env_vars?: Record<string, string>
  }) => api.post(`/api/admin/plugins/config/structured/${pluginId}`, config),
  testPluginConnection: (pluginId: string, config: {
    orchestration?: {
      enabled: boolean
      events: string[]
      condition: { type: string; wake_words?: string[]; keywords?: string[]; threshold?: number }
    }
    settings?: Record<string, any>
    env_vars?: Record<string, string>
  }) => api.post(`/api/admin/plugins/test-connection/${pluginId}`, config),

  // Plugin CRUD
  createPlugin: (data: { plugin_name: string; description: string; events: string[]; plugin_code?: string }) =>
    api.post('/api/admin/plugins/create', data),
  deletePlugin: (pluginId: string, removeFiles: boolean = false) =>
    api.delete(`/api/admin/plugins/${pluginId}`, { params: { remove_files: removeFiles } }),
  writePluginCode: (pluginId: string, data: { code: string; config_yml?: string }) =>
    api.put(`/api/admin/plugins/${pluginId}/code`, data),

  // Plugin AI Assistant (SSE streaming)
  pluginAssistantChat: (messages: Array<{ role: string; content: string }>) => {
    return fetch(`${BACKEND_URL}/api/admin/plugins/assistant`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem(getStorageKey('token'))}`
      },
      body: JSON.stringify({ messages })
    })
  },

  // Plugin Connectivity
  getPluginsConnectivity: () => api.get('/api/admin/plugins/connectivity'),

  // Memory Provider Management
  getMemoryProvider: () => api.get('/api/admin/memory/provider'),
  setMemoryProvider: (provider: string) => api.post('/api/admin/memory/provider', { provider }),

  // LLM Operations Settings
  getLLMOperations: () => api.get('/api/admin/llm-operations'),
  saveLLMOperations: (operations: Record<string, any>) =>
    api.post('/api/admin/llm-operations', operations),
  testLLMModel: (modelName: string | null) =>
    api.post('/api/admin/llm-operations/test', { model_name: modelName }),

  // Network discovery
  getNetworkDiscovery: () => api.get('/api/system/network'),

  // System restart operations
  restartWorkers: () => api.post('/api/admin/system/restart-workers'),
  restartBackend: () => api.post('/api/admin/system/restart-backend'),

  // External service management (host service-manager agent)
  getExternalServices: () => api.get('/api/admin/services'),
  externalServiceAction: (
    name: string,
    action: 'start' | 'stop' | 'restart',
    options?: { build?: boolean; force?: boolean; node?: string | null },
  ) =>
    api.post(`/api/admin/services/${name}/${action}`, options ?? {}),
  setExternalServiceProvider: (
    name: string,
    provider: string,
    build: boolean = false,
    lane: 'batch' | 'streaming' = 'batch',
    node?: string | null,
  ) =>
    api.post(`/api/admin/services/${name}/provider`, { provider, build, lane, node }),
  getExternalServiceOperation: (operationId: string, node?: string | null) =>
    api.get(`/api/admin/services/operations/${operationId}`, { params: node ? { node } : undefined }),

  // Claude remote-control session (control Claude Code from your phone)
  getRemoteControl: () => api.get('/api/admin/remote-control'),
  remoteControlAction: (action: 'start' | 'stop' | 'restart') =>
    api.post(`/api/admin/remote-control/${action}`),

  // Observability
  getObservabilityConfig: () => api.get('/api/observability'),
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

  // Clear jobs
  clearJobs: () => api.delete('/api/queue/jobs'),


  // Plugin events
  getEvents: (limit: number = 50, eventType?: string) => api.get('/api/queue/events', {
    params: { limit, ...(eventType && { event_type: eventType }) }
  }),
  clearEvents: () => api.delete('/api/queue/events'),

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

  uploadFromGDriveFolder: (payload: { gdrive_folder_id: string; device_name?: string }) =>
    api.post('/api/audio/upload_audio_from_gdrive', null, {
      params: {
        gdrive_folder_id: payload.gdrive_folder_id,
        device_name: payload.device_name,
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

  // Streaming chat — OpenAI-compatible completions endpoint
  sendMessage: (message: string, sessionId?: string, includeObsidianMemory?: boolean, memoryLimit?: number, memoryMode?: string) => {
    const requestBody: Record<string, unknown> = {
      messages: [{ role: 'user', content: message }],
      stream: true,
    }
    if (sessionId) {
      requestBody.session_id = sessionId
    }
    if (includeObsidianMemory) {
      requestBody.include_obsidian_memory = includeObsidianMemory
    }
    if (memoryLimit !== undefined) {
      requestBody.memory_limit = memoryLimit
    }
    if (memoryMode) {
      requestBody.memory_mode = memoryMode
    }

    return fetch(`${BACKEND_URL}/api/chat/completions`, {
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

export interface AuditConversation {
  conversation_id: string
  title: string | null
  client_id: string
  created_at: string | null
  duration_seconds: number
  speakers: string[]
  analyzed: boolean
  speech_fraction: number | null
  derived_operation: 'split' | 'merge' | null
  audio_archived: boolean
  audio_archived_at: string | null
  archive_reason: string | null
}

export interface AuditListResponse {
  conversations: AuditConversation[]
  total: number
  limit: number
  offset: number
  scan_capped: boolean
  speech_threshold: number
  // Conversations the Analyze button would process (user's own live audio
  // without cached VAD analysis) — 0 means there is nothing to analyze.
  unanalyzed_count: number
  // Distinct speaker labels present in the scanned working set, so the
  // speaker filter offers only labels that exist in the current view.
  speakers: string[]
}

// In-flight progress published by batch jobs via job.meta "batch_progress".
export interface BatchProgress {
  percent?: number
  message?: string
  done?: number
  total?: number
}

export interface SilenceGap {
  start_seconds: number
  end_seconds: number
  duration_seconds: number
  start_chunk: number
  end_chunk: number
  split_point_seconds: number
}

export interface SilenceGapsResponse {
  analyzed: boolean
  needs_analysis: boolean
  duration_seconds: number
  chunk_duration_seconds: number
  speech_threshold: number
  min_gap_seconds: number
  gaps: SilenceGap[]
}

export interface SpeechRegion {
  start: number
  end: number
}

export interface SpeechRegionsResponse {
  analyzed: boolean
  needs_analysis: boolean
  duration_seconds: number
  speech_seconds: number
  speakers?: string[]
  regions: SpeechRegion[]
}

export interface SplitChild {
  conversation_id: string
  start_seconds: number
  end_seconds: number
  duration_seconds: number
  chunk_count: number
  has_transcript: boolean
  jobs: Record<string, string> | null
}

export interface SplitResponse {
  parent_conversation_id: string
  children: SplitChild[]
}

export interface MergeResponse {
  merged_conversation_id: string
  source_conversation_ids: string[]
  duration_seconds: number
  chunk_count: number
  has_transcript: boolean
  jobs: Record<string, string> | null
}

export interface ExportConversationSummary {
  conversation_id: string
  title: string | null
  client_id: string | null
  clip_count?: number
  clip_seconds?: number
  skipped_reason?: string
}

export interface ExportRecord {
  export_id: string
  created_at: string
  created_by: string
  params: {
    mode?: 'clips' | 'full'
    pad_seconds: number
    speech_threshold: number
    merge_gap_seconds: number
    screened?: boolean
    sensitivity_policy?: string | null
  }
  conversations: ExportConversationSummary[]
  totals: {
    conversation_count: number
    exported_conversations: number
    clip_count: number
    total_clip_seconds: number
    excluded_seconds?: number
    zip_bytes?: number
  }
  zip_ready: boolean
}

// One transcript segment flagged by the privacy screen as too sensitive to share.
export interface ScreenFlaggedSegment {
  index: number
  start: number
  end: number
  category: string
  reason: string
  speaker?: string | null
  text?: string | null
}

export interface ScreenConversationReport {
  conversation_id: string
  title?: string | null
  client_id?: string | null
  segment_count?: number
  flagged?: ScreenFlaggedSegment[]
  flagged_seconds?: number
  skipped_reason?: string
  error?: string
}

export interface ScreenResult {
  success: boolean
  policy: string
  conversations: ScreenConversationReport[]
  totals: { conversation_count: number; flagged_segments: number }
}

export const dataAuditApi = {
  // Enqueue batch VAD analysis. Returns { job_id, status }.
  analyze: (conversationIds?: string[], force: boolean = false) =>
    api.post('/api/data-audit/analyze', {
      conversation_ids: conversationIds ?? null,
      force,
    }),

  // Poll an analysis job's status (includes in-flight batch progress)
  getJobStatus: (jobId: string) =>
    api.get<{ job_id: string; status: string; batch_progress?: BatchProgress }>(
      `/api/queue/jobs/${jobId}/status`
    ),

  // Fetch a job's full record (status + meta progress + result)
  getJobResult: <T = unknown>(jobId: string) =>
    api.get<{
      status: string
      result: T | null
      meta?: { batch_progress?: BatchProgress }
    }>(`/api/queue/jobs/${jobId}`),

  // Filtered listing with VAD speech metrics + speaker labels.
  // Generic param handling so registry filters (filters.tsx) can contribute
  // params without touching this function: undefined/null and empty arrays
  // are dropped, arrays become comma-separated values.
  getConversations: (params: {
    speech_threshold?: number
    min_speech_fraction?: number
    max_speech_fraction?: number
    min_duration?: number
    max_duration?: number
    created_after?: string
    created_before?: string
    include_speakers?: string[]
    exclude_speakers?: string[]
    archived_only?: boolean
    limit?: number
    offset?: number
    [key: string]: unknown
  }) =>
    api.get<AuditListResponse>('/api/data-audit/conversations', {
      params: Object.fromEntries(
        Object.entries(params)
          .filter(([, v]) => v !== undefined && v !== null && !(Array.isArray(v) && v.length === 0))
          .map(([k, v]) => [k, Array.isArray(v) ? v.join(',') : v])
      ),
    }),

  // Archive (hard-delete audio, keep metadata stub)
  archive: (conversationIds: string[], reason: string) =>
    api.post('/api/data-audit/archive', {
      conversation_ids: conversationIds,
      reason,
    }),

  // Detected silence gaps (candidate split points) from cached chunk VAD scores
  getSilenceGaps: (conversationId: string, params?: { speech_threshold?: number; min_gap_seconds?: number }) =>
    api.get<SilenceGapsResponse>(`/api/data-audit/conversations/${conversationId}/silence-gaps`, { params }),

  // Merged speech intervals for speech-skip playback
  getSpeechRegions: (conversationId: string, speakers?: string[]) =>
    api.get<SpeechRegionsResponse>(
      `/api/data-audit/conversations/${conversationId}/speech-regions`,
      { params: speakers?.length ? { speakers: speakers.join(',') } : undefined }
    ),

  // Split a conversation at the given time points (seconds)
  split: (conversationId: string, splitPoints: number[]) =>
    api.post<SplitResponse>(`/api/data-audit/conversations/${conversationId}/split`, {
      split_points: splitPoints,
    }),

  // Merge adjacent conversations into a new one
  merge: (conversationIds: string[]) =>
    api.post<MergeResponse>('/api/data-audit/merge', {
      conversation_ids: conversationIds,
    }),

  // Default shareability policy (prefill for the privacy-screen editor)
  getSensitivityPolicy: () =>
    api.get<{ policy: string }>('/api/data-audit/sensitivity-policy'),

  // Enqueue a privacy screen over the selected conversations.
  // Returns { job_id, status }; fetch the result via getJobResult<ScreenResult>.
  screenExport: (conversationIds: string[], policy?: string) =>
    api.post<{ job_id: string; status: string }>('/api/data-audit/export/screen', {
      conversation_ids: conversationIds,
      policy: policy ?? null,
    }),

  // Enqueue an annotation-dataset export (speech-cropped clips + manifest).
  // `excluded_ranges` (conversation_id → withheld [start,end] ranges from the
  // privacy screen) are carved out of the exported audio + transcript.
  // Returns { job_id, export_id, status }.
  startExport: (
    conversationIds: string[],
    params?: {
      mode?: 'clips' | 'full'
      pad_seconds?: number
      speech_threshold?: number
      merge_gap_seconds?: number
      excluded_ranges?: Record<string, number[][]>
      sensitivity_policy?: string | null
    }
  ) =>
    api.post<{ job_id: string; export_id: string; status: string }>('/api/data-audit/export', {
      conversation_ids: conversationIds,
      ...params,
    }),

  // List completed annotation exports
  listExports: () => api.get<{ exports: ExportRecord[] }>('/api/data-audit/exports'),

  // Direct download URL (token as query param so the browser streams to disk)
  exportDownloadUrl: (exportId: string) => {
    const token = localStorage.getItem(getStorageKey('token')) || ''
    return `${BACKEND_URL}/api/data-audit/exports/${exportId}/download?token=${token}`
  },

  // Delete an export (zip + metadata) from the server
  deleteExport: (exportId: string) => api.delete(`/api/data-audit/exports/${exportId}`),
}

export const knowledgeGraphApi = {
  // Entity operations
  getEntities: (entityType?: string, limit: number = 100) =>
    api.get('/api/knowledge-graph/entities', {
      params: {
        ...(entityType && { entity_type: entityType }),
        limit
      }
    }),

  getEntity: (entityId: string) =>
    api.get(`/api/knowledge-graph/entities/${entityId}`),

  getEntityRelationships: (entityId: string) =>
    api.get(`/api/knowledge-graph/entities/${entityId}/relationships`),

  updateEntity: (entityId: string, data: { name?: string; details?: string; icon?: string }) =>
    api.patch(`/api/knowledge-graph/entities/${entityId}`, data),

  deleteEntity: (entityId: string) =>
    api.delete(`/api/knowledge-graph/entities/${entityId}`),

  // Search
  searchEntities: (query: string, limit: number = 20) =>
    api.get('/api/knowledge-graph/search', {
      params: { query, limit }
    }),

  // Conversation doc browsing
  getConversationDocs: (person?: string, limit: number = 50) =>
    api.get('/api/knowledge-graph/conversations', {
      params: {
        ...(person && { person }),
        limit
      }
    }),

  getPeople: () =>
    api.get('/api/knowledge-graph/people'),

  // Timeline
  getTimeline: (start: string, end: string, limit: number = 100) =>
    api.get('/api/knowledge-graph/timeline', {
      params: { start, end, limit }
    }),

  // Health check
  getHealth: () => api.get('/api/knowledge-graph/health'),
}

// Wake-word data-collection (the "Hermes" training flywheel). Proxied through
// the backend to the standalone wakeword-service.
export interface WakeStream {
  client_id: string
  priming: boolean
  prime_wakeword?: string | null
  armed: boolean
}

// Per-word config the service runs (one section per word in the Wake-Word Lab).
export interface WakeWordConfig {
  name: string
  model: string
  verifier: boolean          // a verifier file is loaded for this word (capability)
  verifier_enabled: boolean  // and it is currently consulted (the runtime toggle)
  threshold: number
  patience: number
  collect_only: boolean
}

export interface WakeStats {
  pending: number
  positive: number
  negative: number
  false_negatives: number
}

export interface WakeSample {
  id: string
  wakeword: string
  bucket: string
  client_id: string
  session_id: string
  score: number
  reason: string
  kind: string
  source: string
  also_fired?: string[]
  sample_rate: number
  created_at_ms: number
  duration_secs: number
  label?: string
  false_negative?: boolean
}

export const wakewordApi = {
  // Configured wake words (+ per-word config). available/active kept for the
  // acoustic-condition picker; wakewords drives the Lab's per-word sections.
  getModels: () =>
    api.get<{ available: string[]; active: string | null; wakewords: WakeWordConfig[] }>(
      '/api/wakeword/models'
    ),
  getStreams: () => api.get<{ streams: WakeStream[] }>('/api/wakeword/streams'),
  // Toggle a word between normal dispatch and collect-only (shadow) mode. Admin
  // only; effective live and persisted across restarts. Returns refreshed config.
  setCollectOnly: (wakeword: string, collect_only: boolean) =>
    api.post<{ wakewords: WakeWordConfig[] }>('/api/wakeword/collect_only', {
      wakeword,
      collect_only,
    }),
  // Toggle a word's second-stage verifier on/off. Admin only; the verifier stays
  // loaded (disabling falls back to stage-1). Effective live and persisted.
  setVerifierEnabled: (wakeword: string, enabled: boolean) =>
    api.post<{ wakewords: WakeWordConfig[] }>('/api/wakeword/verifier_enabled', {
      wakeword,
      enabled,
    }),
  // Enroll a specific word. No client_id -> backend primes the caller's recorder.
  prime: (wakeword: string, client_id?: string) =>
    api.post('/api/wakeword/prime', { wakeword, ...(client_id ? { client_id } : {}) }),
  // Manually end an in-progress prime; the captured attempt is saved to pending.
  unprime: (client_id?: string) =>
    api.post('/api/wakeword/unprime', client_id ? { client_id } : {}),
  getSamples: (wakeword: string, bucket: 'pending' | 'positive' | 'negative') =>
    api.get<{ wakeword: string; bucket: string; samples: WakeSample[] }>(
      '/api/wakeword/samples',
      { params: { wakeword, bucket } }
    ),
  // Per-word stats: { "hey_hermes": {pending, positive, negative, false_negatives}, ... }
  getStats: () => api.get<Record<string, WakeStats>>('/api/wakeword/samples/stats'),
  // Remove exact-duplicate clips within a wake word (keeps one per group).
  dedupe: (wakeword: string) =>
    api.post<Record<string, { duplicate_groups: number; removed: number; removed_by_bucket: Record<string, number>; kept_unique: number; conflicts: number }>>(
      '/api/wakeword/samples/dedupe', null, { params: { wakeword } }
    ),
  getAudioBlob: (id: string) =>
    api.get(`/api/wakeword/samples/${encodeURIComponent(id)}/audio`, {
      responseType: 'blob',
    }),
  label: (id: string, label: 'wake' | 'not_wake') =>
    api.post(`/api/wakeword/samples/${encodeURIComponent(id)}/label`, { label }),
  // Move a clip to a different wake word's bucket (default pending) — for the
  // overlap case (a bare "hermes" that armed hey_hermes by priority).
  move: (id: string, wakeword: string, bucket: 'pending' | 'positive' | 'negative' = 'pending') =>
    api.post(`/api/wakeword/samples/${encodeURIComponent(id)}/move`, null, {
      params: { wakeword, bucket },
    }),
  // Copy a clip into another word's bucket (source stays) — a shared FP that fired
  // multiple words is a hard negative for each.
  copy: (id: string, wakeword: string, bucket: 'pending' | 'positive' | 'negative' = 'pending') =>
    api.post(`/api/wakeword/samples/${encodeURIComponent(id)}/copy`, null, {
      params: { wakeword, bucket },
    }),
  remove: (id: string) => api.delete(`/api/wakeword/samples/${encodeURIComponent(id)}`),
}
