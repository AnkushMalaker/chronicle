import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Clock,
  Play,
  Pause,
  CheckCircle,
  XCircle,
  RotateCcw,
  StopCircle,
  Eye,
  Filter,
  X,
  RefreshCw,
  Layers,
  Trash2,
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  FileAudio,
  FileText,
  Brain,
  Repeat,
  Zap
} from 'lucide-react';
import { queueApi } from '../services/api';

interface QueueStats {
  total_jobs: number;
  queued_jobs: number;
  started_jobs: number;  // RQ standard, not "processing_jobs"
  finished_jobs: number;  // RQ standard, not "completed_jobs"
  failed_jobs: number;
  canceled_jobs: number;  // RQ standard (US spelling), not "cancelled_jobs"
  deferred_jobs: number;
  timestamp: string;
}

interface Filters {
  status: string;
  job_type: string;
  priority: string;
}

interface StreamingSession {
  session_id: string;
  user_id: string;
  client_id: string;
  provider: string;
  mode: string;
  status: string;
  chunks_published: number;
  started_at: number;
  last_chunk_at: number;
  age_seconds: number;
  idle_seconds: number;
  conversation_count?: number;
  // Speech detection events
  last_event?: string;
  speech_detected_at?: string;
  speaker_check_status?: string;
  identified_speakers?: string;
}

interface StreamConsumer {
  name: string;
  pending: number;
  idle_ms: number;
}

interface StreamConsumerGroup {
  name: string;
  consumers: StreamConsumer[];
  pending: number;
}

interface StreamHealth {
  stream_length?: number;
  consumer_groups?: StreamConsumerGroup[];
  total_pending?: number;
  error?: string;
  exists?: boolean;
}

interface CompletedSession {
  session_id: string;
  client_id: string;
  conversation_id: string | null;
  has_conversation: boolean;
  action: string;
  reason: string;
  completed_at: number;
  audio_file: string;
}

interface StreamingStatus {
  active_sessions: StreamingSession[];  // Kept for backward compatibility
  completed_sessions: CompletedSession[];
  stream_health: {
    [streamKey: string]: StreamHealth & {
      stream_age_seconds?: number;
    };
  };
  rq_queues: {
    [queue: string]: {
      count: number;
      failed_count: number;
    };
  };
  timestamp: number;
}

interface EventRecord {
  timestamp: number;
  event: string;
  user_id: string;
  plugins_subscribed: string[];
  plugins_executed: Array<{ plugin_id: string; success: boolean; message: string }>;
  metadata: Record<string, any>;
}

// Known event type colors — unknown types fall back to gray via getEventColor()
const EVENT_TYPE_COLORS: Record<string, string> = {
  'conversation.complete': 'bg-green-100 text-green-700',
  'transcript.batch': 'bg-blue-100 text-blue-700',
  'memory.processed': 'bg-purple-100 text-purple-700',
  'button.single_press': 'bg-orange-100 text-orange-700',
  'button.double_press': 'bg-orange-100 text-orange-700',
  'plugin_action': 'bg-indigo-100 text-indigo-700',
};
const DEFAULT_EVENT_COLOR = 'bg-gray-100 text-gray-700';
const getEventColor = (eventType: string) => EVENT_TYPE_COLORS[eventType] || DEFAULT_EVENT_COLOR;

const Queue: React.FC = () => {
  const [jobs, setJobs] = useState<any[]>([]);
  const [stats, setStats] = useState<QueueStats | null>(null);
  const [streamingStatus, setStreamingStatus] = useState<StreamingStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedJob, setSelectedJob] = useState<any | null>(null);
  const [loadingJobDetails, setLoadingJobDetails] = useState(false);
  const [filters, setFilters] = useState<Filters>({
    status: '',
    job_type: '',
    priority: ''
  });
  const [pagination, setPagination] = useState({
    offset: 0,
    limit: 20,
    total: 0,
    has_more: false
  });
  const [refreshing, setRefreshing] = useState(false);
  const [showFlushModal, setShowFlushModal] = useState(false);
  const [flushSettings, setFlushSettings] = useState({
    older_than_hours: 24,
    statuses: ['finished', 'failed'],  // RQ standard status names
    flush_all: false,
    include_failed: false,  // For flush_all mode
    include_completed: false  // For flush_all mode (note: API expects include_completed for backward compat)
  });
  const [flushing, setFlushing] = useState(false);
  const [expandedConversations, setExpandedConversations] = useState<Set<string>>(new Set());
  const [expandedJobs, setExpandedJobs] = useState<Set<string>>(new Set());
  const [conversationJobs, setConversationJobs] = useState<{[conversationId: string]: any[]}>({});
  const [lastUpdate, setLastUpdate] = useState<number>(Date.now());
  const [selectedEvent, setSelectedEvent] = useState<EventRecord | null>(null);
  const [autoRefreshEnabled, setAutoRefreshEnabled] = useState<boolean>(() => {
    // Load from localStorage, default to true
    const saved = localStorage.getItem('queue_auto_refresh');
    return saved !== null ? saved === 'true' : true;
  });

  // System events
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [eventFilters, setEventFilters] = useState<Record<string, 'include' | 'exclude'>>({});

  const cycleEventFilter = (eventType: string) => {
    setEventFilters(prev => {
      const current = prev[eventType];
      const next = { ...prev };
      if (!current) {
        next[eventType] = 'include';
      } else if (current === 'include') {
        next[eventType] = 'exclude';
      } else {
        delete next[eventType];
      }
      return next;
    });
  };
  const [eventsExpanded, setEventsExpanded] = useState<boolean>(() => {
    const saved = localStorage.getItem('queue_events_expanded');
    return saved !== null ? saved === 'true' : true;
  });

  // Completed conversations pagination
  const [completedConvPage, setCompletedConvPage] = useState(1);
  const [completedConvItemsPerPage] = useState(10);
  const [completedConvTimeRange, setCompletedConvTimeRange] = useState(24); // hours

  // Use refs to track current state in interval
  const expandedConversationsRef = useRef<Set<string>>(new Set());
  const streamingStatusRef = useRef<StreamingStatus | null>(null);
  const refreshingRef = useRef<boolean>(false);

  // Update refs when state changes
  useEffect(() => {
    expandedConversationsRef.current = expandedConversations;
  }, [expandedConversations]);

  useEffect(() => {
    streamingStatusRef.current = streamingStatus;
  }, [streamingStatus]);

  useEffect(() => {
    refreshingRef.current = refreshing;
  }, [refreshing]);

  // Main data fetch function - uses consolidated dashboard endpoint
  const fetchData = useCallback(async () => {
    if (refreshingRef.current) {
      return;
    }

    setRefreshing(true);

    try {
      const currentExpanded = expandedConversationsRef.current;
      const expandedConversationIds = Array.from(currentExpanded);

      // Single API call to get all dashboard data
      const response = await queueApi.getDashboard(expandedConversationIds);
      const dashboardData = response.data;

      // Extract jobs from response (using RQ standard status names)
      const queuedJobs = dashboardData.jobs.queued || [];
      const startedJobs = dashboardData.jobs.started || [];  // RQ standard, not "processing"
      const finishedJobs = dashboardData.jobs.finished || [];  // RQ standard, not "completed"
      const failedJobs = dashboardData.jobs.failed || [];

      // Combine all jobs
      const allFetchedJobs = [...queuedJobs, ...startedJobs, ...finishedJobs, ...failedJobs];

      // Group jobs by conversation_id (primary identifier for conversations)
      const jobsByConversation: {[conversationId: string]: any[]} = {};

      allFetchedJobs.forEach(job => {
        if (!job || !job.job_id) return; // Skip invalid jobs

        // Extract conversation_id from metadata
        const conversationId = job.meta?.conversation_id;
        if (conversationId) {
          if (!jobsByConversation[conversationId]) {
            jobsByConversation[conversationId] = [];
          }
          jobsByConversation[conversationId].push(job);

          // Debug logging for grouping
        } else {
          // Only log warning for non-session-level jobs
          // Audio persistence jobs are expected to not have conversation_id
          // Job has no conversation_id - cannot group (expected for session-level and audio persistence jobs)
        }
      });

      // Merge conversation jobs from dashboard response (for backward compatibility, check both session_jobs and conversation_jobs)
      const dashboardConvJobs = dashboardData.conversation_jobs || dashboardData.session_jobs;
      if (dashboardConvJobs) {
        Object.entries(dashboardConvJobs).forEach(([conversationId, jobs]: [string, any]) => {
          // Merge with existing jobs and deduplicate by job_id
          const existingJobs = jobsByConversation[conversationId] || [];
          const existingJobIds = new Set(existingJobs.map((j: any) => j.job_id));
          const newJobs = jobs.filter((j: any) => !existingJobIds.has(j.job_id));
          jobsByConversation[conversationId] = [...existingJobs, ...newJobs];
        });
      }

      // Update state
      setJobs(allFetchedJobs);
      setConversationJobs(jobsByConversation);
      setStats(dashboardData.stats);
      setStreamingStatus(dashboardData.streaming_status);
      setEvents(dashboardData.events || []);
      setLastUpdate(Date.now());

      // Auto-expand active conversations (those with open_conversation_job in progress)
      const newExpanded = new Set(expandedConversations);
      const newExpandedJobs = new Set(expandedJobs);
      let expandedCount = 0;
      let expandedJobsCount = 0;

      // Find all conversations with active open_conversation_job
      Object.entries(jobsByConversation).forEach(([_conversationId, jobs]) => {
        const openConvJob = jobs.find((j: any) => j.job_type === 'open_conversation_job');
        if (openConvJob && openConvJob.status === 'started') {
          const conversationId = openConvJob.meta?.conversation_id;
          if (conversationId && !expandedConversations.has(conversationId)) {
            newExpanded.add(conversationId);
            expandedCount++;
          }

          // Also expand all job cards in active conversations
          jobs.forEach((job: any) => {
            if (!expandedJobs.has(job.job_id)) {
              newExpandedJobs.add(job.job_id);
              expandedJobsCount++;
            }
          });
        }
      });

      if (expandedCount > 0) {
        setExpandedConversations(newExpanded);
      }

      if (expandedJobsCount > 0) {
        setExpandedJobs(newExpandedJobs);
      }
    } catch (error: any) {
      console.error('❌ Error fetching dashboard data:', error);

      // If it's a 401 error, stop auto-refresh to prevent repeated failed requests
      if (error?.response?.status === 401) {
        console.warn('🔐 Authentication error detected - disabling auto-refresh');
        setAutoRefreshEnabled(false);
      }
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  // Save auto-refresh preference to localStorage
  useEffect(() => {
    localStorage.setItem('queue_auto_refresh', autoRefreshEnabled.toString());
  }, [autoRefreshEnabled]);

  // Auto-refresh interval using useRef
  useEffect(() => {
    if (!autoRefreshEnabled) {
      return;
    }

    const intervalId = setInterval(() => {
      fetchData();
    }, 5000); // Refresh every 5 seconds

    return () => {
      clearInterval(intervalId);
    };
  }, [fetchData, autoRefreshEnabled]);

  // Initial data fetch
  useEffect(() => {
    fetchData();
  }, [filters, pagination.offset, fetchData]);


  const viewJobDetails = async (jobId: string) => {
    setLoadingJobDetails(true);
    try {
      const response = await queueApi.getJob(jobId);
      setSelectedJob(response.data);
    } catch (error) {
      console.error('Error fetching job details:', error);
      alert('Failed to fetch job details');
    } finally {
      setLoadingJobDetails(false);
    }
  };

  // ESC key handler for modals
  useEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (selectedEvent) {
          setSelectedEvent(null);
        } else if (selectedJob) {
          setSelectedJob(null);
        } else if (showFlushModal) {
          setShowFlushModal(false);
        }
      }
    };

    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [selectedJob, showFlushModal, selectedEvent]);

  // Commented out - keeping for future use
  // const retryJob = async (jobId: string) => {
  //   try {
  //     await queueApi.retryJob(jobId, false);
  //     fetchData();
  //   } catch (error) {
  //     console.error('Error retrying job:', error);
  //   }
  // };

  // const cancelJob = async (jobId: string) => {
  //   if (!confirm('Are you sure you want to cancel this job?')) return;

  //   try {
  //     await queueApi.cancelJob(jobId);
  //     fetchData();
  //   } catch (error) {
  //     console.error('Error cancelling job:', error);
  //   }
  // };

  const cleanupStuckWorkers = async () => {
    if (!confirm('This will clean up all stuck workers and pending messages. Continue?')) return;

    try {
      const response = await queueApi.cleanupStuckWorkers();
      const data = response.data;

      alert(`✅ Cleanup complete!\n\nTotal cleaned: ${data.total_cleaned} messages\n\n${
        Object.entries(data.providers).map(([provider, result]: [string, any]) =>
          `${provider}: ${result.message || result.error || 'Unknown'}`
        ).join('\n')
      }`);

      // Refresh data to show updated counts
      fetchData();
    } catch (error: any) {
      console.error('❌ Error during cleanup:', error);
      alert(`Failed to cleanup workers: ${error.response?.data?.error || error.message}`);
    }
  };

  const cleanupOldSessions = async () => {
    if (!confirm('This will remove old and stuck "finalizing" sessions from the dashboard. Continue?')) return;

    try {
      const response = await queueApi.cleanupOldSessions(3600); // 1 hour
      const data = response.data;

      alert(`✅ Cleanup complete!\n\nRemoved ${data.cleaned_count} old session(s)`);

      // Refresh data to show updated counts
      fetchData();
    } catch (error: any) {
      console.error('❌ Error during cleanup:', error);
      alert(`Failed to cleanup sessions: ${error.response?.data?.error || error.message}`);
    }
  };

  const applyFilters = () => {
    setPagination(prev => ({ ...prev, offset: 0 }));
    fetchData();
  };

  const clearFilters = () => {
    setFilters({ status: '', job_type: '', priority: '' });
    setPagination(prev => ({ ...prev, offset: 0 }));
  };


  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'queued': return <Clock className="w-4 h-4" />;
      case 'started': return <Play className="w-4 h-4 animate-pulse" />;  // RQ standard
      case 'finished': return <CheckCircle className="w-4 h-4" />;  // RQ standard
      case 'failed': return <XCircle className="w-4 h-4" />;
      case 'canceled': return <StopCircle className="w-4 h-4" />;  // RQ standard (US spelling)
      case 'deferred': return <Pause className="w-4 h-4" />;
      case 'scheduled': return <Pause className="w-4 h-4" />;  // RQ standard, not "waiting"
      default: return <Clock className="w-4 h-4" />;
    }
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case 'queued': return 'text-yellow-600 bg-yellow-100';
      case 'started': return 'text-blue-600 bg-blue-100';  // RQ standard
      case 'finished': return 'text-green-600 bg-green-100';  // RQ standard
      case 'failed': return 'text-red-600 bg-red-100';
      case 'canceled': return 'text-gray-600 bg-gray-100';  // RQ standard (US spelling)
      case 'deferred': return 'text-blue-600 bg-blue-100';
      case 'scheduled': return 'text-blue-600 bg-blue-100';  // RQ standard, not "waiting"
      default: return 'text-gray-600 bg-gray-100';
    }
  };

  const getJobTypeIcon = (type: string) => {
    const iconClass = "w-3.5 h-3.5";
    switch (type) {
      case 'audio_transcription':
      case 'process_audio_chunk':
        return <FileAudio className={iconClass} />;
      case 'transcript_processing':
      case 'reprocess_transcript':
        return <FileText className={iconClass} />;
      case 'memory_extraction':
      case 'reprocess_memory':
        return <Brain className={iconClass} />;
      case 'process_audio_files':
      case 'process_single_audio_file':
        return <Zap className={iconClass} />;
      default:
        return <Repeat className={iconClass} />;
    }
  };

  const getJobTypeColor = (type: string, status: string) => {
    // Safety check for undefined/null values
    if (!type || !status) {
      return { bgColor: 'bg-gray-400', borderColor: 'border-gray-500' };
    }

    // Base colors by job type
    let bgColor = 'bg-gray-400';
    let borderColor = 'border-gray-500';

    // Transcription jobs - blue shades
    if (type.includes('transcribe') || type === 'transcribe_full_audio_job') {
      bgColor = 'bg-blue-500';
      borderColor = 'border-blue-600';
    }
    // Speaker recognition - purple shades
    else if (type.includes('speaker') || type.includes('recognise') || type === 'recognise_speakers_job') {
      bgColor = 'bg-purple-500';
      borderColor = 'border-purple-600';
    }
    // Memory jobs - pink shades
    else if (type.includes('memory') || type === 'process_memory_job') {
      bgColor = 'bg-pink-500';
      borderColor = 'border-pink-600';
    }
    // Conversation/open jobs - cyan shades (check this AFTER memory to avoid confusion)
    else if (type.includes('conversation') || type.includes('open_conversation') || type === 'open_conversation_job') {
      bgColor = 'bg-cyan-500';
      borderColor = 'border-cyan-600';
    }
    // Speech detection jobs - green shades
    else if (type.includes('speech') || type.includes('detect')) {
      bgColor = 'bg-green-500';
      borderColor = 'border-green-600';
    }
    // Audio processing - orange shades
    else if (type.includes('audio') || type.includes('persist')) {
      bgColor = 'bg-orange-500';
      borderColor = 'border-orange-600';
    }
    // Default - gray
    else {
      bgColor = 'bg-gray-400';
      borderColor = 'border-gray-500';
    }

    // Failed jobs - always red
    if (status === 'failed') {
      bgColor = 'bg-red-500';
      borderColor = 'border-red-600';
    }
    // Processing jobs - add pulse animation
    else if (status === 'started') {
      bgColor = bgColor + ' animate-pulse';
    }

    return { bgColor, borderColor };
  };


  const flushJobs = async () => {
    setFlushing(true);
    try {
      const body = flushSettings.flush_all
        ? {
            confirm: true,
            include_failed: flushSettings.include_failed,
            include_completed: flushSettings.include_completed
          }
        : {
            older_than_hours: flushSettings.older_than_hours,
            statuses: flushSettings.statuses
          };

      const response = await queueApi.flushJobs(flushSettings.flush_all, body);
      alert(`Successfully flushed ${response.data.total_removed} jobs!`);
      setShowFlushModal(false);
      fetchData(); // Refresh the data
    } catch (error: any) {
      console.error('Error flushing jobs:', error);
      if (error.response?.status === 403) {
        alert('Admin access required to flush jobs');
      } else if (error.response?.status === 401) {
        localStorage.removeItem('token');
        window.location.href = '/login';
      } else {
        alert(`Failed to flush jobs: ${error.response?.data?.detail || error.message}`);
      }
    } finally {
      setFlushing(false);
    }
  };

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleString();
  };

  const getJobTypeShort = (jobType: string) => {
    const typeMap: {[key: string]: string} = {
      'open_conversation_job': 'Open Conv',
      'stream_speech_detection_job': 'Speech Detect',
      'enroll_speakers_job': 'Speaker Enroll',
      'check_enrolled_speakers_job': 'Check Speakers',
      'audio_persistence_job': 'Audio Persist',
      'process_transcription_job': 'Transcribe',
      'process_memory_job': 'Memory',
      'crop_audio_job': 'Crop Audio'
    };
    return typeMap[jobType] || jobType;
  };

  const retryJob = async (jobId: string) => {
    try {
      await queueApi.retryJob(jobId);
      fetchData();
    } catch (error) {
      console.error('Failed to retry job:', error);
    }
  };

  const cancelJob = async (jobId: string) => {
    try {
      await queueApi.cancelJob(jobId);
      fetchData();
    } catch (error) {
      console.error('Failed to cancel job:', error);
    }
  };

  const prevPage = () => {
    setPagination(prev => ({
      ...prev,
      offset: Math.max(0, prev.offset - prev.limit)
    }));
  };

  const nextPage = () => {
    setPagination(prev => ({
      ...prev,
      offset: prev.offset + prev.limit
    }));
  };

  const formatDuration = (job: any) => {
    if (!job.started_at) return '-';

    const start = new Date(job.started_at).getTime();
    // For failed/finished jobs, use completed_at or ended_at. For running jobs, use current time.
    const end = job.completed_at || job.ended_at
      ? new Date((job.completed_at || job.ended_at)!).getTime()
      : (job.status === 'started' ? Date.now() : start); // Don't show increasing time for failed jobs
    const durationMs = end - start;

    if (durationMs < 1000) return `${durationMs}ms`;
    if (durationMs < 60000) return `${(durationMs / 1000).toFixed(1)}s`;
    if (durationMs < 3600000) return `${Math.floor(durationMs / 60000)}m ${Math.floor((durationMs % 60000) / 1000)}s`;
    return `${Math.floor(durationMs / 3600000)}h ${Math.floor((durationMs % 3600000) / 60000)}m`;
  };

  const toggleConversationExpansion = (conversationId: string) => {
    const newExpanded = new Set(expandedConversations);

    if (newExpanded.has(conversationId)) {
      // Collapse
      newExpanded.delete(conversationId);
      setExpandedConversations(newExpanded);
    } else {
      // Expand and trigger refresh to fetch jobs via dashboard endpoint
      newExpanded.add(conversationId);
      setExpandedConversations(newExpanded);

      // Trigger a refresh if jobs not already loaded
      if (!conversationJobs[conversationId]) {
        fetchData();
      }
    }
  };

  const toggleJobExpansion = (jobId: string) => {
    const newExpanded = new Set(expandedJobs);
    if (newExpanded.has(jobId)) {
      newExpanded.delete(jobId);
    } else {
      newExpanded.add(jobId);
    }
    setExpandedJobs(newExpanded);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex justify-between items-center">
        <div className="flex items-center space-x-3">
          <Layers className="w-6 h-6 text-blue-600" />
          <div>
            <h1 className="text-2xl font-bold text-gray-900">Queue & Events</h1>
            <p className="text-xs text-gray-500">
              Last updated: {new Date(lastUpdate).toLocaleTimeString()} • Auto-refresh every 2s
            </p>
          </div>
        </div>
        <div className="flex items-center space-x-2">
          <button
            onClick={() => setAutoRefreshEnabled(!autoRefreshEnabled)}
            className={`flex items-center space-x-2 px-4 py-2 rounded-lg transition-colors ${
              autoRefreshEnabled
                ? 'bg-green-600 hover:bg-green-700 text-white'
                : 'bg-gray-600 hover:bg-gray-700 text-white'
            }`}
            title={autoRefreshEnabled ? 'Auto-refresh enabled (click to disable)' : 'Auto-refresh disabled (click to enable)'}
          >
            {autoRefreshEnabled ? (
              <>
                <Pause className="w-4 h-4" />
                <span>Auto-refresh ON</span>
              </>
            ) : (
              <>
                <Play className="w-4 h-4" />
                <span>Auto-refresh OFF</span>
              </>
            )}
          </button>
          <button
            onClick={() => setShowFlushModal(true)}
            className="flex items-center space-x-2 px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700"
          >
            <Trash2 className="w-4 h-4" />
            <span>Flush Jobs</span>
          </button>
          <button
            onClick={() => fetchData()}
            disabled={refreshing}
            className="flex items-center space-x-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
          >
            <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
            <span>Refresh</span>
          </button>
        </div>
      </div>

      {/* Stats Cards */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-4">
          <div className="bg-white rounded-lg border p-4">
            <div className="flex items-center space-x-2">
              <Layers className="w-5 h-5 text-gray-600" />
              <div>
                <p className="text-sm text-gray-600">Total</p>
                <p className="text-xl font-semibold">{stats.total_jobs}</p>
              </div>
            </div>
          </div>

          <div className="bg-white rounded-lg border p-4">
            <div className="flex items-center space-x-2">
              <Clock className="w-5 h-5 text-yellow-600" />
              <div>
                <p className="text-sm text-gray-600">Queued</p>
                <p className="text-xl font-semibold text-yellow-600">{stats.queued_jobs}</p>
              </div>
            </div>
          </div>

          <div className="bg-white rounded-lg border p-4">
            <div className="flex items-center space-x-2">
              <Play className={`w-5 h-5 text-blue-600 ${stats.started_jobs > 0 ? 'animate-pulse' : ''}`} />
              <div>
                <p className="text-sm text-gray-600">Started</p>
                <p className="text-xl font-semibold text-blue-600">{stats.started_jobs}</p>
              </div>
            </div>
          </div>

          <div className="bg-white rounded-lg border p-4">
            <div className="flex items-center space-x-2">
              <CheckCircle className="w-5 h-5 text-green-600" />
              <div>
                <p className="text-sm text-gray-600">Finished</p>
                <p className="text-xl font-semibold text-green-600">{stats.finished_jobs}</p>
              </div>
            </div>
          </div>

          <div className="bg-white rounded-lg border p-4">
            <div className="flex items-center space-x-2">
              <XCircle className="w-5 h-5 text-red-600" />
              <div>
                <p className="text-sm text-gray-600">Failed</p>
                <p className="text-xl font-semibold text-red-600">{stats.failed_jobs}</p>
              </div>
            </div>
          </div>

          <div className="bg-white rounded-lg border p-4">
            <div className="flex items-center space-x-2">
              <StopCircle className="w-5 h-5 text-gray-600" />
              <div>
                <p className="text-sm text-gray-600">Canceled</p>
                <p className="text-xl font-semibold text-gray-600">{stats.canceled_jobs}</p>
              </div>
            </div>
          </div>

          <div className="bg-white rounded-lg border p-4">
            <div className="flex items-center space-x-2">
              <Pause className="w-5 h-5 text-blue-600" />
              <div>
                <p className="text-sm text-gray-600">Deferred</p>
                <p className="text-xl font-semibold text-blue-600">{stats.deferred_jobs}</p>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Streaming Status */}
      {streamingStatus && (
        <div className="bg-white rounded-lg border overflow-hidden">
          <div className="px-6 py-4 border-b border-gray-200 flex justify-between items-center">
            <h3 className="text-lg font-medium">Audio Streaming & Conversations</h3>
            <div className="flex items-center space-x-2">
              <button
                onClick={cleanupOldSessions}
                className="flex items-center space-x-2 px-4 py-2 bg-orange-600 text-white rounded-md hover:bg-orange-700 transition-colors text-sm"
                title="Remove old sessions (>1 hour old)"
              >
                <RotateCcw className="w-4 h-4" />
                <span>Cleanup Old Sessions</span>
              </button>
              {streamingStatus?.stream_health && Object.keys(streamingStatus.stream_health).length > 0 && (
                <button
                  onClick={async () => {
                    const streamCount = Object.keys(streamingStatus.stream_health).length;
                    if (!streamingStatus || !confirm(`Remove ALL ${streamCount} active streams? This will force-delete all streams including actively streaming ones.`)) return;

                    try {
                      const response = await queueApi.cleanupOldSessions(0); // 0 seconds = all sessions
                      const data = response.data;
                      alert(`✅ Removed ${data.cleaned_count} stream(s)`);
                      fetchData();
                    } catch (error: any) {
                      console.error('❌ Error removing streams:', error);
                      alert(`Failed to remove streams: ${error.response?.data?.error || error.message}`);
                    }
                  }}
                  className="flex items-center space-x-2 px-4 py-2 bg-red-600 text-white rounded-md hover:bg-red-700 transition-colors text-sm"
                  title="Force remove ALL active streams"
                >
                  <Trash2 className="w-4 h-4" />
                  <span>Remove All Streams ({Object.keys(streamingStatus.stream_health).length})</span>
                </button>
              )}
              <button
                onClick={cleanupStuckWorkers}
                className="flex items-center space-x-2 px-4 py-2 bg-yellow-600 text-white rounded-md hover:bg-yellow-700 transition-colors text-sm"
                title="Clean up stuck workers and pending messages"
              >
                <RotateCcw className="w-4 h-4" />
                <span>Cleanup Stuck Workers{streamingStatus?.stream_health && Object.values(streamingStatus.stream_health).some((s: any) => s.total_pending > 0) && ` (${
                  Object.values(streamingStatus.stream_health).reduce((sum: number, s: any) => sum + (s.total_pending || 0), 0)
                })`}</span>
              </button>
            </div>
          </div>

          <div className="p-6 space-y-6">
            {/* Stream Workers Section - Shows audio streams + listen jobs */}
            <div>
              <h4 className="text-sm font-medium text-gray-700 mb-3">Stream Workers (Client Sessions)</h4>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {streamingStatus?.stream_health && Object.entries(streamingStatus.stream_health).map(([streamKey, health]) => {
                  // Extract client_id from stream key (format: audio:stream:{client_id})
                  const clientId = streamKey.replace('audio:stream:', '');

                  // Find all listen jobs for this client with deduplication
                  const allJobsRaw = Object.values(conversationJobs).flat().filter(job => job != null);

                  // Deduplicate by job_id
                  const jobMap = new Map();
                  allJobsRaw.forEach((job: any) => {
                    if (job && job.job_id) {
                      jobMap.set(job.job_id, job);
                    }
                  });
                  const allJobs = Array.from(jobMap.values());

                  // Get all listen jobs for this client (only active/queued/processing, not completed)
                  const allListenJobs = allJobs.filter((job: any) =>
                    job && job.job_type === 'stream_speech_detection_job' &&
                    job.meta?.client_id === clientId &&
                    job.status !== 'finished' &&
                    job.status !== 'failed'
                  );

                  // Show only the LATEST active speech detection job (most recent created_at)
                  // Completed ones have already exited and shouldn't be shown here
                  const listenJobs = allListenJobs.length > 0
                    ? [allListenJobs.sort((a, b) =>
                        new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
                      )[0]]
                    : [];

                  return (
                    <div key={streamKey} className="p-4 bg-gray-50 rounded-lg border border-gray-200">
                      <div className="flex items-center justify-between mb-2">
                        <span className="text-sm font-medium">{streamKey}</span>
                        <span className="text-xs px-2 py-0.5 bg-green-100 text-green-700 rounded">Active</span>
                      </div>

                      <div className="space-y-2">
                        <div className="flex justify-between text-xs">
                          <span className="text-gray-600">Stream Length:</span>
                          <span className="font-medium">{health.stream_length}</span>
                        </div>
                        <div className="flex justify-between text-xs">
                          <span className="text-gray-600">Age:</span>
                          <span className="font-medium">{(health.stream_age_seconds || 0).toFixed(0)}s</span>
                        </div>
                        <div className="flex justify-between text-xs">
                          <span className="text-gray-600">Pending:</span>
                          <span className={`font-medium ${health.total_pending && health.total_pending > 0 ? 'text-yellow-600' : 'text-green-600'}`}>
                            {health.total_pending}
                          </span>
                        </div>
                        {health.consumer_groups && health.consumer_groups.map((group) => (
                          <div key={group.name} className="mt-2 pt-2 border-t border-gray-200">
                            <div className="text-xs text-gray-600 mb-1">{group.name}:</div>
                            {group.consumers.map((consumer) => (
                              <div key={consumer.name} className="flex justify-between text-xs pl-2">
                                <span className="text-gray-700 truncate">{consumer.name}</span>
                                <span className={consumer.pending > 0 ? 'text-yellow-600' : 'text-green-600'}>
                                  {consumer.pending} pending
                                </span>
                              </div>
                            ))}
                          </div>
                        ))}

                        {/* Current Speech Detection Job */}
                        {listenJobs.length > 0 && (
                          <div className="mt-2 pt-2 border-t border-gray-200">
                            <div className="text-xs text-gray-600 mb-1">Current Speech Detection:</div>
                            {listenJobs.map((job) => {
                              const runtime = job.started_at
                                ? Math.floor((Date.now() - new Date(job.started_at).getTime()) / 1000)
                                : 0;
                              const minutes = Math.floor(runtime / 60);
                              const seconds = runtime % 60;

                              return (
                                <div key={job.job_id} className="bg-white rounded p-2 space-y-1">
                                  <div className="flex items-center justify-between">
                                    <div className="flex items-center space-x-1">
                                      {getStatusIcon(job.status)}
                                      <span className="text-gray-700 font-medium text-xs">{job.job_type}</span>
                                      <span className={`px-1 py-0.5 rounded text-xs ${getStatusColor(job.status)}`}>
                                        {job.status}
                                      </span>
                                    </div>
                                    <button
                                      onClick={() => viewJobDetails(job.job_id)}
                                      className="text-indigo-600 hover:text-indigo-900 flex-shrink-0"
                                    >
                                      <Eye className="w-3 h-3" />
                                    </button>
                                  </div>

                                  {/* Job metadata */}
                                  <div className="text-xs text-gray-600 space-y-0.5 pl-4">
                                    <div className="flex justify-between">
                                      <span>Job ID:</span>
                                      <span className="font-mono text-gray-800">{job.job_id.substring(0, 12)}...</span>
                                    </div>
                                    {job.started_at && (
                                      <div className="flex justify-between">
                                        <span>Runtime:</span>
                                        <span className="font-medium text-gray-800">{minutes}m {seconds}s</span>
                                      </div>
                                    )}
                                    {job.created_at && (
                                      <div className="flex justify-between">
                                        <span>Created:</span>
                                        <span className="text-gray-800">{new Date(job.created_at).toLocaleTimeString()}</span>
                                      </div>
                                    )}
                                    {job.meta?.speech_detected_at && (
                                      <div className="flex justify-between">
                                        <span>Speech Detected:</span>
                                        <span className="text-green-700 font-medium">{new Date(job.meta.speech_detected_at).toLocaleString()}</span>
                                      </div>
                                    )}
                                    {job.meta?.status && (
                                      <div className="flex justify-between">
                                        <span>Status:</span>
                                        <span className="text-blue-700 font-medium">{job.meta.status.replace(/_/g, ' ')}</span>
                                      </div>
                                    )}
                                  </div>

                                  {/* Session Events */}
                                  {(() => {
                                    const session = streamingStatus?.active_sessions?.find((s: StreamingSession) => s.session_id === job.meta?.session_id);
                                    if (!session) return null;

                                    return (
                                      <div className="text-xs space-y-1 pl-4 mt-2 pt-2 border-t border-gray-200">
                                        <div className="font-semibold text-gray-700 mb-1">Speech Detection Events:</div>
                                        {session.last_event && (
                                          <div className="flex justify-between">
                                            <span className="text-gray-600">Last Event:</span>
                                            <span className="text-gray-800 font-mono text-xs">{session.last_event.split(':')[0]}</span>
                                          </div>
                                        )}
                                        {session.speaker_check_status && (
                                          <div className="flex justify-between">
                                            <span className="text-gray-600">Speaker Check:</span>
                                            <span className={`font-medium ${
                                              session.speaker_check_status === 'enrolled' ? 'text-green-700' :
                                              session.speaker_check_status === 'checking' ? 'text-blue-700' :
                                              session.speaker_check_status === 'failed' ? 'text-red-700' :
                                              session.speaker_check_status === 'timeout' ? 'text-yellow-700' :
                                              'text-gray-700'
                                            }`}>{session.speaker_check_status}</span>
                                          </div>
                                        )}
                                        {session.identified_speakers && (
                                          <div className="flex justify-between">
                                            <span className="text-gray-600">Speakers:</span>
                                            <span className="text-green-700 font-medium">{session.identified_speakers}</span>
                                          </div>
                                        )}
                                      </div>
                                    );
                                  })()}
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Active and Completed Conversations Grid */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              {/* Active Conversations - Grouped by conversation_id */}
              <div>
                <h4 className="text-sm font-medium text-gray-700 mb-3">Active Conversations</h4>
                {(() => {
                  // Group all jobs by conversation_id with deduplication
                  const allJobsRaw = Object.values(conversationJobs).flat().filter(job => job != null);

                  // Deduplicate by job_id
                  const jobMap = new Map();
                  allJobsRaw.forEach((job: any) => {
                    if (job && job.job_id) {
                      jobMap.set(job.job_id, job);
                    }
                  });
                  const allJobs = Array.from(jobMap.values());

                  // Group ALL jobs by conversation_id (regardless of status)
                  const allConversationJobs = new Map<string, any[]>();

                  // Group jobs by conversation_id only
                  // EXCLUDE session-level jobs (like audio persistence)
                  allJobs.forEach(job => {
                    if (!job) return;

                    // Skip session-level jobs (they run for entire session, not per conversation)
                    // Also skip audio persistence jobs by job_type
                    if (job.meta?.session_level === true || job.job_type === 'audio_streaming_persistence_job') {
                      return;
                    }

                    const conversationId = job.meta?.conversation_id;
                    if (conversationId) {
                      if (!allConversationJobs.has(conversationId)) {
                        allConversationJobs.set(conversationId, []);
                      }
                      allConversationJobs.get(conversationId)!.push(job);
                    }
                  });

                  // Filter to only show conversations where at least one job is NOT completed
                  const conversationMap = new Map<string, any[]>();
                  allConversationJobs.forEach((jobs, conversationId) => {
                    const hasActiveJob = jobs.some(j => j.status !== 'finished' && j.status !== 'failed');
                    if (hasActiveJob) {
                      conversationMap.set(conversationId, jobs);
                    }
                  });

                  if (conversationMap.size === 0) {
                    return (
                      <div className="text-center py-8 text-gray-500 text-sm bg-gray-50 rounded-lg border border-gray-200">
                        No active conversations
                      </div>
                    );
                  }

                  return (
                    <div className="space-y-2">
                      {Array.from(conversationMap.entries()).map(([conversationId, jobs]) => {
                        const isExpanded = expandedConversations.has(conversationId);

                        // Find the open_conversation_job for metadata, or fallback to any job with metadata
                        const openConvJob = jobs.find(j => j.job_type === 'open_conversation_job');
                        const fallbackJob = jobs.find(j => j.meta && Object.keys(j.meta).length > 0);
                        const meta = openConvJob?.meta || fallbackJob?.meta || {};

                        // Extract conversation info
                        const clientId = meta.client_id || 'Unknown';
                        const transcript = meta.transcript || '';
                        const speakers = meta.speakers || [];
                        const wordCount = meta.word_count || 0;
                        const lastUpdate = meta.last_update || '';
                        const createdAt = openConvJob?.created_at || null;

                        // Check if any jobs have failed
                        const hasFailedJob = jobs.some(j => j.status === 'failed');
                        const failedJobCount = jobs.filter(j => j.status === 'failed').length;

                        return (
                          <div key={conversationId} className={`rounded-lg border overflow-hidden ${hasFailedJob ? 'bg-red-50 border-red-300' : 'bg-cyan-50 border-cyan-200'}`}>
                            <div
                              className={`flex items-center justify-between p-3 cursor-pointer transition-colors ${hasFailedJob ? 'hover:bg-red-100' : 'hover:bg-cyan-100'}`}
                              onClick={() => toggleConversationExpansion(conversationId)}
                            >
                              <div className="flex-1">
                                <div className="flex items-center space-x-2">
                                  {isExpanded ? (
                                    <ChevronDown className={`w-4 h-4 ${hasFailedJob ? 'text-red-600' : 'text-cyan-600'}`} />
                                  ) : (
                                    <ChevronRight className={`w-4 h-4 ${hasFailedJob ? 'text-red-600' : 'text-cyan-600'}`} />
                                  )}
                                  {hasFailedJob ? (
                                    <AlertTriangle className="w-4 h-4 text-red-600" />
                                  ) : (
                                    <Brain className="w-4 h-4 text-cyan-600 animate-pulse" />
                                  )}
                                  <span className="text-sm font-medium text-gray-900">{clientId}</span>
                                  {hasFailedJob ? (
                                    <span className="text-xs px-2 py-0.5 bg-red-200 text-red-800 rounded font-medium">
                                      {failedJobCount} Error{failedJobCount > 1 ? 's' : ''}
                                    </span>
                                  ) : (
                                    <span className="text-xs px-2 py-0.5 bg-cyan-100 text-cyan-700 rounded">Active</span>
                                  )}
                                  {speakers.length > 0 && (
                                    <span className="text-xs px-2 py-0.5 bg-purple-100 text-purple-700 rounded">
                                      {speakers.length} speaker{speakers.length > 1 ? 's' : ''}
                                    </span>
                                  )}
                                </div>
                                <div className="mt-1 text-xs text-gray-600">
                                  Conversation: {conversationId.substring(0, 8)}... •
                                  {createdAt && `Started: ${new Date(createdAt).toLocaleTimeString()} • `}
                                  Words: {wordCount}
                                  {lastUpdate && ` • Updated: ${new Date(lastUpdate).toLocaleTimeString()}`}
                                </div>
                                {transcript && (
                                  <div className="mt-1 text-xs text-gray-700 italic truncate">
                                    "{transcript.substring(0, 100)}{transcript.length > 100 ? '...' : ''}"
                                  </div>
                                )}
                              </div>
                            </div>

                          {/* Expanded Jobs Section */}
                          {isExpanded && (
                            <div className="border-t border-cyan-200 bg-white p-3">
                              {/* Pipeline Timeline */}
                              <div className="mb-4">
                                <h5 className="text-xs font-medium text-gray-700 mb-3">Pipeline Timeline:</h5>
                                {(() => {
                                  // Helper function to get display name from job type
                                  const getJobDisplayName = (jobType: string) => {
                                    const nameMap: { [key: string]: string } = {
                                      'stream_speech_detection_job': 'Speech',
                                      'open_conversation_job': 'Open',
                                      'transcribe_full_audio_job': 'Transcript',
                                      'recognise_speakers_job': 'Speakers',
                                      'process_memory_job': 'Memory'
                                    };
                                    return nameMap[jobType] || jobType.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
                                  };

                                  // Helper function to get icon for job type
                                  const getJobIcon = (jobType: string) => {
                                    if (jobType.includes('speech') || jobType.includes('detect')) return Brain;
                                    if (jobType.includes('conversation') || jobType.includes('open')) return Brain;
                                    if (jobType.includes('transcribe')) return FileText;
                                    if (jobType.includes('speaker') || jobType.includes('recognise')) return Brain;
                                    if (jobType.includes('memory')) return Brain;
                                    return Brain; // Default icon
                                  };

                                  // Build dynamic pipeline from actual jobs with timing data
                                  // Sort by start time to show chronological order
                                  const jobsWithTiming = jobs
                                    .filter(j => j && j.started_at)
                                    .map(job => {
                                      const startTime = new Date(job.started_at!).getTime();
                                      const endTime = job.completed_at || job.ended_at
                                        ? new Date((job.completed_at || job.ended_at)!).getTime()
                                        : (job.status === 'started' ? Date.now() : startTime);

                                      return {
                                        job,
                                        startTime,
                                        endTime,
                                        duration: (endTime - startTime) / 1000,
                                        name: getJobDisplayName(job.job_type),
                                        icon: getJobIcon(job.job_type)
                                      };
                                    })
                                    .sort((a, b) => a.startTime - b.startTime);

                                  const jobTimes = jobsWithTiming;

                                  // Find earliest start and latest end
                                  const validTimes = jobTimes.filter(t => t !== null);
                                  if (validTimes.length === 0) {
                                    return (
                                      <div className="text-xs text-gray-500 italic">No job timing data available</div>
                                    );
                                  }

                                  const earliestStart = Math.min(...validTimes.map(t => t!.startTime));
                                  const latestEnd = Math.max(...validTimes.map(t => t!.endTime));
                                  const totalDuration = (latestEnd - earliestStart) / 1000; // in seconds

                                  // Format duration for display
                                  const formatDuration = (seconds: number) => {
                                    if (seconds < 1) return `${(seconds * 1000).toFixed(0)}ms`;
                                    if (seconds < 60) return `${seconds.toFixed(1)}s`;
                                    const mins = Math.floor(seconds / 60);
                                    const secs = Math.floor(seconds % 60);
                                    return `${mins}m ${secs}s`;
                                  };

                                  // Generate time axis markers (0%, 25%, 50%, 75%, 100%)
                                  const timeMarkers = [0, 0.25, 0.5, 0.75, 1].map(pct => ({
                                    percent: pct * 100,
                                    time: formatDuration(totalDuration * pct)
                                  }));

                                  return (
                                    <div className="space-y-2">
                                      {/* Time axis */}
                                      <div className="relative h-4 border-b border-gray-300">
                                        {timeMarkers.map((marker, idx) => (
                                          <div
                                            key={idx}
                                            className="absolute"
                                            style={{ left: `${marker.percent}%`, transform: 'translateX(-50%)' }}
                                          >
                                            <div className="w-px h-2 bg-gray-400"></div>
                                            <div className="text-xs text-gray-500 mt-0.5 whitespace-nowrap">
                                              {marker.time}
                                            </div>
                                          </div>
                                        ))}
                                      </div>

                                      {/* Job timeline bars */}
                                      <div className="space-y-2 mt-6">
                                        {jobTimes.map((jobTime) => {
                                          const { job, startTime, endTime, duration, name, icon: Icon } = jobTime;

                                          // Calculate position and width as percentage of total timeline
                                          const startPercent = ((startTime - earliestStart) / (latestEnd - earliestStart)) * 100;
                                          const widthPercent = ((endTime - startTime) / (latestEnd - earliestStart)) * 100;

                                          // Use job type colors
                                          const jobColors = getJobTypeColor(job.job_type, job.status);
                                          const barColor = jobColors.bgColor;
                                          const borderColor = jobColors.borderColor;

                                          return (
                                            <div key={job.job_id} className="flex items-center space-x-2 h-8">
                                              {/* Stage Icon */}
                                              <div className={`w-8 h-8 rounded-full border-2 ${borderColor} ${barColor} flex items-center justify-center flex-shrink-0`}>
                                                <Icon className="w-4 h-4 text-white" />
                                              </div>

                                              {/* Stage Name */}
                                              <span className="text-xs text-gray-700 w-20 flex-shrink-0">{name}</span>

                                              {/* Timeline Container */}
                                              <div className="flex-1 relative h-6 bg-gray-100 rounded">
                                                {/* Job Bar */}
                                                <div
                                                  className={`absolute h-6 rounded ${barColor} ${job.status === 'started' ? 'animate-pulse' : ''} flex items-center justify-center`}
                                                  style={{
                                                    left: `${startPercent}%`,
                                                    width: `${widthPercent}%`
                                                  }}
                                                  title={`Started: ${new Date(startTime).toLocaleTimeString()}\nDuration: ${formatDuration(duration)}`}
                                                >
                                                  <span className="text-xs text-white font-medium px-2 truncate">
                                                    {formatDuration(duration)}
                                                  </span>
                                                </div>
                                              </div>
                                            </div>
                                          );
                                        })}
                                      </div>

                                      {/* Total Duration */}
                                      <div className="text-xs text-gray-600 text-right mt-2">
                                        Total: {formatDuration(totalDuration)}
                                      </div>
                                    </div>
                                  );
                                })()}
                              </div>

                              <h5 className="text-xs font-medium text-gray-700 mb-2">Conversation Jobs:</h5>
                              {jobs.filter(j => j != null && j.job_id).length > 0 ? (
                                <div className="space-y-1">
                                  {jobs
                                    .filter(j => j != null && j.job_id)
                                    .sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime())
                                    .map((job, index) => (
                                    <div key={job.job_id} className={`p-2 bg-gray-50 rounded border ${getJobTypeColor(job.job_type, job.status).borderColor}`} style={{ borderLeftWidth: '12px' }}>
                                      <div
                                        className="flex items-center justify-between cursor-pointer hover:bg-gray-100 transition-colors rounded px-1 py-0.5"
                                        onClick={() => toggleJobExpansion(job.job_id)}
                                      >
                                        <div className="flex-1 min-w-0">
                                          <div className="flex items-center space-x-2">
                                            <span className="text-xs font-mono text-gray-500 flex-shrink-0">#{index + 1}</span>
                                            <span className="flex-shrink-0">{getJobTypeIcon(job.job_type)}</span>
                                            <span className="flex-shrink-0">{getStatusIcon(job.status)}</span>
                                            <span className="text-xs font-medium text-gray-900 truncate">{job.job_type}</span>
                                            <span className={`text-xs px-1.5 py-0.5 rounded ${getStatusColor(job.status)}`}>
                                              {job.status}
                                            </span>
                                            <span className="text-xs text-gray-500">{job.queue}</span>
                                            {/* Show memory count badge on collapsed card */}
                                            {!expandedJobs.has(job.job_id) && job.job_type === 'process_memory_job' && job.result?.memories_created !== undefined && (
                                              <span className="text-xs px-1.5 py-0.5 bg-pink-100 text-pink-700 rounded">
                                                {job.result.memories_created} memories
                                              </span>
                                            )}
                                          </div>
                                        </div>
                                      </div>

                                      {/* Collapsible metadata section */}
                                      {expandedJobs.has(job.job_id) && (
                                        <div className="mt-1 text-xs text-gray-600 space-y-0.5">
                                          <div>
                                            {job.started_at && (
                                              <span>Started: {new Date(job.started_at).toLocaleTimeString()}</span>
                                            )}
                                            {job.started_at && (
                                              <span> • Duration: {formatDuration(job)}</span>
                                            )}
                                          </div>

                                          {/* Show job-specific metadata */}
                                          {job.meta && (
                                            <div className="space-y-0.5 pl-2 border-l-2 border-gray-300">
                                              {/* open_conversation_job metadata */}
                                              {job.job_type === 'open_conversation_job' && (
                                                <>
                                                  {job.meta.word_count !== undefined && (
                                                    <div>Words: <span className="font-medium">{job.meta.word_count}</span></div>
                                                  )}
                                                  {job.meta.speakers && job.meta.speakers.length > 0 && (
                                                    <div>Speakers: <span className="font-medium">{job.meta.speakers.join(', ')}</span></div>
                                                  )}
                                                  {job.meta.inactivity_seconds !== undefined && (
                                                    <div>Idle: <span className="font-medium">{Math.floor(job.meta.inactivity_seconds)}s</span></div>
                                                  )}
                                                  {job.meta.transcript && (
                                                    <div className="italic text-gray-500 truncate max-w-md">
                                                      "{job.meta.transcript.substring(0, 80)}..."
                                                    </div>
                                                  )}
                                                </>
                                              )}

                                              {/* transcribe_full_audio_job batch progress */}
                                              {job.job_type === 'transcribe_full_audio_job' && job.status === 'started' && job.meta?.batch_progress && (
                                                <div className="mt-1">
                                                  <div className="flex items-center justify-between text-xs mb-1">
                                                    <span className="text-blue-700">{job.meta.batch_progress.message}</span>
                                                    <span className="text-blue-600 font-medium">{job.meta.batch_progress.percent}%</span>
                                                  </div>
                                                  <div className="w-full bg-blue-200 rounded-full h-1.5">
                                                    <div className="bg-blue-600 h-1.5 rounded-full transition-all duration-300" style={{ width: `${job.meta.batch_progress.percent}%` }} />
                                                  </div>
                                                </div>
                                              )}

                                              {/* transcribe_full_audio_job metadata */}
                                              {job.job_type === 'transcribe_full_audio_job' && job.result && (
                                                <>
                                                  {job.result.transcript && (
                                                    <div>Transcript: <span className="font-medium">{job.result.transcript.length} chars</span></div>
                                                  )}
                                                  {job.result.processing_time_seconds && (
                                                    <div>Processing: <span className="font-medium">{job.result.processing_time_seconds.toFixed(1)}s</span></div>
                                                  )}
                                                </>
                                              )}

                                              {/* recognise_speakers_job metadata */}
                                              {job.job_type === 'recognise_speakers_job' && job.result && (
                                                <>
                                                  {job.result.identified_speakers && job.result.identified_speakers.length > 0 && (
                                                    <div>Identified: <span className="font-medium">{job.result.identified_speakers.join(', ')}</span></div>
                                                  )}
                                                  {job.result.segment_count && (
                                                    <div>Segments: <span className="font-medium">{job.result.segment_count}</span></div>
                                                  )}
                                                </>
                                              )}

                                              {/* process_memory_job metadata */}
                                              {job.job_type === 'process_memory_job' && job.meta && (
                                                <>
                                                  {job.meta.memories_created !== undefined && (
                                                    <div>Memories: <span className="font-medium">{job.meta.memories_created} created</span></div>
                                                  )}
                                                  {job.meta.processing_time && (
                                                    <div>Processing: <span className="font-medium">{job.meta.processing_time.toFixed(1)}s</span></div>
                                                  )}
                                                  {job.meta.memory_details && job.meta.memory_details.length > 0 && (
                                                    <div className="mt-2">
                                                      <div className="text-xs font-medium text-gray-700 mb-1">Memories Created:</div>
                                                      {job.meta.memory_details.map((memory: any, idx: number) => (
                                                        <div key={idx} className="text-xs bg-pink-50 p-2 rounded mb-1">
                                                          "{memory.text}"
                                                        </div>
                                                      ))}
                                                    </div>
                                                  )}
                                                </>
                                              )}

                                              {/* Show conversation_id if present */}
                                              {job.meta.conversation_id && (
                                                <div className="font-mono text-gray-500">
                                                  Conv: {job.meta.conversation_id.substring(0, 8)}...
                                                </div>
                                              )}
                                            </div>
                                          )}
                                        </div>
                                      )}
                                      <button
                                        onClick={(e) => {
                                          e.stopPropagation();
                                          viewJobDetails(job.job_id);
                                        }}
                                        className="ml-2 text-indigo-600 hover:text-indigo-900 flex-shrink-0"
                                      >
                                        <Eye className="w-3 h-3" />
                                      </button>
                                    </div>
                                  ))}
                                </div>
                              ) : (
                                <div className="text-xs text-gray-500 italic">No jobs found for this conversation</div>
                              )}
                            </div>
                          )}
                        </div>
                        );
                      })}
                    </div>
                  );
                })()}
              </div>

              {/* Completed Conversations - Grouped by conversation_id */}
              <div>
                <div className="flex items-center justify-between mb-3">
                  <h4 className="text-sm font-medium text-gray-700">Completed Conversations</h4>
                  <div className="flex items-center space-x-2">
                    <label className="text-xs text-gray-600">Time range:</label>
                    <select
                      value={completedConvTimeRange}
                      onChange={(e) => {
                        setCompletedConvTimeRange(Number(e.target.value));
                        setCompletedConvPage(1); // Reset to first page
                      }}
                      className="text-xs border border-gray-300 rounded px-2 py-1"
                    >
                      <option value={1}>Last 1 hour</option>
                      <option value={6}>Last 6 hours</option>
                      <option value={24}>Last 24 hours</option>
                      <option value={168}>Last 7 days</option>
                    </select>
                  </div>
                </div>
                {(() => {
                  // Group all jobs by conversation_id for completed conversations with deduplication
                  const allJobsRaw = Object.values(conversationJobs).flat().filter(job => job != null);

                  // Deduplicate by job_id
                  const jobMap = new Map();
                  allJobsRaw.forEach((job: any) => {
                    if (job && job.job_id) {
                      jobMap.set(job.job_id, job);
                    }
                  });
                  const allJobs = Array.from(jobMap.values());

                  // Group ALL jobs by conversation_id (regardless of status)
                  const allConversationJobs = new Map<string, any[]>();

                  // Group jobs by conversation_id only
                  // EXCLUDE session-level jobs (like audio persistence)
                  allJobs.forEach(job => {
                    if (!job) return;

                    // Skip session-level jobs (they run for entire session, not per conversation)
                    // Also skip audio persistence jobs by job_type
                    if (job.meta?.session_level === true || job.job_type === 'audio_streaming_persistence_job') {
                      return;
                    }

                    const conversationId = job.meta?.conversation_id;
                    if (conversationId) {
                      if (!allConversationJobs.has(conversationId)) {
                        allConversationJobs.set(conversationId, []);
                      }
                      allConversationJobs.get(conversationId)!.push(job);
                    }
                  });

                  // Filter to only show conversations where ALL jobs are completed or failed
                  const conversationMap = new Map<string, any[]>();
                  allConversationJobs.forEach((jobs, conversationId) => {
                    const allJobsComplete = jobs.every(j => j.status === 'finished' || j.status === 'failed');
                    if (allJobsComplete) {
                      conversationMap.set(conversationId, jobs);
                    }
                  });

                  if (conversationMap.size === 0) {
                    return (
                      <div className="text-center py-8 text-gray-500 text-sm bg-gray-50 rounded-lg border border-gray-200">
                        No completed conversations
                      </div>
                    );
                  }

                  // Convert to array and filter by time range
                  const now = Date.now();
                  const timeRangeMs = completedConvTimeRange * 60 * 60 * 1000; // hours to milliseconds

                  let conversationsArray = Array.from(conversationMap.entries())
                    .map(([conversationId, jobs]) => {
                      // Find the open_conversation_job for created_at
                      const openConvJob = jobs.find(j => j.job_type === 'open_conversation_job');
                      const createdAt = openConvJob?.created_at ? new Date(openConvJob.created_at).getTime() : 0;
                      return { conversationId, jobs, createdAt };
                    })
                    .filter(({ createdAt }) => {
                      // Filter by time range
                      return createdAt > 0 && (now - createdAt) <= timeRangeMs;
                    })
                    .sort((a, b) => b.createdAt - a.createdAt); // Most recent first

                  // Apply pagination
                  const totalConversations = conversationsArray.length;
                  const totalPages = Math.ceil(totalConversations / completedConvItemsPerPage);
                  const startIndex = (completedConvPage - 1) * completedConvItemsPerPage;
                  const endIndex = startIndex + completedConvItemsPerPage;
                  const paginatedConversations = conversationsArray.slice(startIndex, endIndex);

                  if (conversationsArray.length === 0) {
                    return (
                      <div className="text-center py-8 text-gray-500 text-sm bg-gray-50 rounded-lg border border-gray-200">
                        No completed conversations in the selected time range
                      </div>
                    );
                  }

                  return (
                    <>
                      <div className="space-y-2">
                        {paginatedConversations.map(({ conversationId, jobs }) => {
                        const isExpanded = expandedConversations.has(conversationId);

                        // Find the open_conversation_job for metadata, or fallback to any job with metadata
                        const openConvJob = jobs.find(j => j.job_type === 'open_conversation_job');
                        const fallbackJob = jobs.find(j => j.meta && Object.keys(j.meta).length > 0);
                        const meta = openConvJob?.meta || fallbackJob?.meta || {};

                        // Find transcription job for title/summary
                        const transcriptionJob = jobs.find(j => j.job_type === 'transcribe_full_audio_job');
                        const transcriptionMeta = transcriptionJob?.meta || {};

                        // Extract conversation info from metadata
                        const clientId = meta.client_id || 'Unknown';
                        const transcript = meta.transcript || '';
                        const speakers = meta.speakers || [];
                        const wordCount = meta.word_count || 0;
                        const createdAt = openConvJob?.created_at || null;
                        const title = transcriptionMeta.title || null;
                        const summary = transcriptionMeta.summary || null;

                        // Check job statuses
                        const allComplete = jobs.every(j => j.status === 'finished');
                        const hasFailedJob = jobs.some(j => j.status === 'failed');
                        const failedJobCount = jobs.filter(j => j.status === 'failed').length;

                        // Determine status styling
                        let bgColor = 'bg-yellow-50 border-yellow-200';
                        let hoverColor = 'hover:bg-yellow-100';
                        let iconColor = 'text-yellow-600';
                        let statusBadge = 'bg-yellow-100 text-yellow-700';
                        let statusText = 'Processing';
                        let StatusIcon = Clock;

                        if (hasFailedJob) {
                          bgColor = 'bg-red-50 border-red-300';
                          hoverColor = 'hover:bg-red-100';
                          iconColor = 'text-red-600';
                          statusBadge = 'bg-red-200 text-red-800';
                          statusText = `${failedJobCount} Error${failedJobCount > 1 ? 's' : ''}`;
                          StatusIcon = AlertTriangle;
                        } else if (allComplete) {
                          bgColor = 'bg-green-50 border-green-200';
                          hoverColor = 'hover:bg-green-100';
                          iconColor = 'text-green-600';
                          statusBadge = 'bg-green-100 text-green-700';
                          statusText = 'Complete';
                          StatusIcon = CheckCircle;
                        }

                        return (
                          <div key={conversationId} className={`rounded-lg border overflow-hidden ${bgColor}`}>
                            <div
                              className={`flex items-center justify-between p-3 cursor-pointer transition-colors ${hoverColor}`}
                              onClick={() => toggleConversationExpansion(conversationId)}
                            >
                              <div className="flex-1">
                                <div className="flex items-center space-x-2">
                                  {isExpanded ? (
                                    <ChevronDown className={`w-4 h-4 ${iconColor}`} />
                                  ) : (
                                    <ChevronRight className={`w-4 h-4 ${iconColor}`} />
                                  )}
                                  <StatusIcon className={`w-4 h-4 ${iconColor}`} />
                                  <span className="text-sm font-medium text-gray-900">{clientId}</span>
                                  <span className={`text-xs px-2 py-0.5 rounded font-medium ${statusBadge}`}>
                                    {statusText}
                                  </span>
                                  {speakers.length > 0 && (
                                    <span className="text-xs px-2 py-0.5 bg-purple-100 text-purple-700 rounded">
                                      {speakers.length} speaker{speakers.length > 1 ? 's' : ''}
                                    </span>
                                  )}
                                </div>
                                <div className="mt-1 text-xs text-gray-600">
                                  Conversation: {conversationId.substring(0, 8)}... •
                                  Words: {wordCount}
                                  {createdAt && (
                                    <> • Created: {new Date(createdAt).toLocaleString()}</>
                                  )}
                                </div>
                                {/* Show title/summary for completed, or transcript for in-progress or when no title exists */}
                                {allComplete ? (
                                  <>
                                    {title ? (
                                      <div className="mt-1 text-sm font-medium text-gray-900">
                                        {title}
                                      </div>
                                    ) : transcript ? (
                                      <div className="mt-1 text-xs text-gray-700 italic truncate">
                                        "{transcript.substring(0, 100)}{transcript.length > 100 ? '...' : ''}"
                                      </div>
                                    ) : null}
                                    {summary && (
                                      <div className="mt-1 text-xs text-gray-700 italic">
                                        {summary}
                                      </div>
                                    )}
                                  </>
                                ) : (
                                  transcript && (
                                    <div className="mt-1 text-xs text-gray-700 italic truncate">
                                      "{transcript.substring(0, 100)}{transcript.length > 100 ? '...' : ''}"
                                    </div>
                                  )
                                )}
                              </div>
                            </div>

                            {/* Expanded Jobs Section */}
                            {isExpanded && (
                              <div className={`border-t bg-white p-3 ${
                                allComplete ? 'border-green-200' : 'border-yellow-200'
                              }`}>
                                {/* Pipeline Timeline */}
                                <div className="mb-4">
                                  <h5 className="text-xs font-medium text-gray-700 mb-3">Pipeline Timeline:</h5>
                                  {(() => {
                                    // Helper function to get display name from job type
                                    const getJobDisplayName = (jobType: string) => {
                                      const nameMap: { [key: string]: string } = {
                                        'stream_speech_detection_job': 'Speech',
                                        'open_conversation_job': 'Open',
                                        'transcribe_full_audio_job': 'Transcript',
                                        'recognise_speakers_job': 'Speakers',
                                        'process_memory_job': 'Memory'
                                      };
                                      return nameMap[jobType] || jobType.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
                                    };

                                    // Helper function to get icon for job type
                                    const getJobIcon = (jobType: string) => {
                                      if (jobType.includes('speech') || jobType.includes('detect')) return Brain;
                                      if (jobType.includes('conversation') || jobType.includes('open')) return Brain;
                                      if (jobType.includes('transcribe')) return FileText;
                                      if (jobType.includes('speaker') || jobType.includes('recognise')) return Brain;
                                      if (jobType.includes('memory')) return Brain;
                                      return Brain; // Default icon
                                    };

                                    // Build dynamic pipeline from actual jobs with timing data
                                    // Sort by start time to show chronological order
                                    const jobsWithTiming = jobs
                                      .filter(j => j && j.started_at)
                                      .map(job => {
                                        const startTime = new Date(job.started_at!).getTime();
                                        const endTime = job.completed_at || job.ended_at
                                          ? new Date((job.completed_at || job.ended_at)!).getTime()
                                          : (job.status === 'started' ? Date.now() : startTime);

                                        return {
                                          job,
                                          startTime,
                                          endTime,
                                          duration: (endTime - startTime) / 1000,
                                          name: getJobDisplayName(job.job_type),
                                          icon: getJobIcon(job.job_type)
                                        };
                                      })
                                      .sort((a, b) => a.startTime - b.startTime);

                                    const jobTimes = jobsWithTiming;

                                    // Find earliest start and latest end
                                    const validTimes = jobTimes.filter(t => t !== null);
                                    if (validTimes.length === 0) {
                                      return (
                                        <div className="text-xs text-gray-500 italic">No job timing data available</div>
                                      );
                                    }

                                    const earliestStart = Math.min(...validTimes.map(t => t!.startTime));
                                    const latestEnd = Math.max(...validTimes.map(t => t!.endTime));
                                    const totalDuration = (latestEnd - earliestStart) / 1000; // in seconds

                                    // Format duration for display
                                    const formatDuration = (seconds: number) => {
                                      if (seconds < 1) return `${(seconds * 1000).toFixed(0)}ms`;
                                      if (seconds < 60) return `${seconds.toFixed(1)}s`;
                                      const mins = Math.floor(seconds / 60);
                                      const secs = Math.floor(seconds % 60);
                                      return `${mins}m ${secs}s`;
                                    };

                                    // Generate time axis markers (0%, 25%, 50%, 75%, 100%)
                                    const timeMarkers = [0, 0.25, 0.5, 0.75, 1].map(pct => ({
                                      percent: pct * 100,
                                      time: formatDuration(totalDuration * pct)
                                    }));

                                    return (
                                      <div className="space-y-2">
                                        {/* Time axis */}
                                        <div className="relative h-4 border-b border-gray-300">
                                          {timeMarkers.map((marker, idx) => (
                                            <div
                                              key={idx}
                                              className="absolute"
                                              style={{ left: `${marker.percent}%`, transform: 'translateX(-50%)' }}
                                            >
                                              <div className="w-px h-2 bg-gray-400"></div>
                                              <div className="text-xs text-gray-500 mt-0.5 whitespace-nowrap">
                                                {marker.time}
                                              </div>
                                            </div>
                                          ))}
                                        </div>

                                        {/* Job timeline bars */}
                                        <div className="space-y-2 mt-6">
                                          {jobTimes.map((jobTime) => {
                                            const { job, startTime, endTime, duration, name, icon: Icon } = jobTime;

                                            // Calculate position and width as percentage of total timeline
                                            const startPercent = ((startTime - earliestStart) / (latestEnd - earliestStart)) * 100;
                                            const widthPercent = ((endTime - startTime) / (latestEnd - earliestStart)) * 100;

                                            // Use job type colors
                                            const jobColors = getJobTypeColor(job.job_type, job.status);
                                            const barColor = jobColors.bgColor;
                                            const borderColor = jobColors.borderColor;

                                            return (
                                              <div key={job.job_id} className="flex items-center space-x-2 h-8">
                                                {/* Stage Icon */}
                                                <div className={`w-8 h-8 rounded-full border-2 ${borderColor} ${barColor} flex items-center justify-center flex-shrink-0`}>
                                                  <Icon className="w-4 h-4 text-white" />
                                                </div>

                                                {/* Stage Name */}
                                                <span className="text-xs text-gray-700 w-20 flex-shrink-0">{name}</span>

                                                {/* Timeline Container */}
                                                <div className="flex-1 relative h-6 bg-gray-100 rounded">
                                                  {/* Job Bar */}
                                                  <div
                                                    className={`absolute h-6 rounded ${barColor} ${job.status === 'started' ? 'animate-pulse' : ''} flex items-center justify-center`}
                                                    style={{
                                                      left: `${startPercent}%`,
                                                      width: `${widthPercent}%`
                                                    }}
                                                    title={`Started: ${new Date(startTime).toLocaleTimeString()}\nDuration: ${formatDuration(duration)}${job.meta?.batch_progress ? `\n${job.meta.batch_progress.message}` : ''}`}
                                                  >
                                                    <span className="text-xs text-white font-medium px-2 truncate">
                                                      {job.status === 'started' && job.meta?.batch_progress
                                                        ? `${job.meta.batch_progress.current}/${job.meta.batch_progress.total}`
                                                        : formatDuration(duration)}
                                                    </span>
                                                  </div>
                                                </div>
                                              </div>
                                            );
                                          })}
                                        </div>

                                        {/* Total Duration */}
                                        <div className="text-xs text-gray-600 text-right mt-2">
                                          Total: {formatDuration(totalDuration)}
                                        </div>
                                      </div>
                                    );
                                  })()}
                                </div>

                                <h5 className="text-xs font-medium text-gray-700 mb-2">Conversation Jobs:</h5>
                                {jobs.filter(j => j != null && j.job_id).length > 0 ? (
                                  <div className="space-y-1">
                                    {jobs
                                      .filter(j => j != null && j.job_id)
                                      .sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime())
                                      .map((job, index) => (
                                      <div key={job.job_id} className={`p-2 bg-gray-50 rounded border ${getJobTypeColor(job.job_type, job.status).borderColor}`} style={{ borderLeftWidth: '12px' }}>
                                        <div className="flex items-center justify-between">
                                          <div
                                            className="flex-1 flex items-center space-x-2 cursor-pointer hover:bg-gray-100 transition-colors rounded px-1 py-0.5"
                                            onClick={() => toggleJobExpansion(job.job_id)}
                                          >
                                            <span className="text-xs font-mono text-gray-500 flex-shrink-0">#{index + 1}</span>
                                            <span className="flex-shrink-0">{getJobTypeIcon(job.job_type)}</span>
                                            <span className="flex-shrink-0">{getStatusIcon(job.status)}</span>
                                            <span className="text-xs font-medium text-gray-900 truncate">{job.job_type}</span>
                                            <span className={`text-xs px-1.5 py-0.5 rounded ${getStatusColor(job.status)}`}>
                                              {job.status}
                                            </span>
                                            <span className="text-xs text-gray-500">{job.queue || job.data?.queue || 'unknown'}</span>
                                            {/* Show memory count badge on collapsed card */}
                                            {!expandedJobs.has(job.job_id) && job.job_type === 'process_memory_job' && job.result?.memories_created !== undefined && (
                                              <span className="text-xs px-1.5 py-0.5 bg-pink-100 text-pink-700 rounded">
                                                {job.result.memories_created} memories
                                              </span>
                                            )}
                                          </div>
                                          <button
                                            onClick={(e) => {
                                              e.stopPropagation();
                                              viewJobDetails(job.job_id);
                                            }}
                                            className="ml-2 text-indigo-600 hover:text-indigo-900 flex-shrink-0"
                                          >
                                            <Eye className="w-3 h-3" />
                                          </button>
                                        </div>

                                        {/* Collapsible metadata section */}
                                        {expandedJobs.has(job.job_id) && (
                                          <div className="mt-1 text-xs text-gray-600 space-y-0.5 pl-4">
                                            <div>
                                              {job.started_at && (
                                                <span>Started: {new Date(job.started_at).toLocaleTimeString()}</span>
                                              )}
                                              {job.started_at && (
                                                <span> • Duration: {formatDuration(job)}</span>
                                              )}
                                            </div>

                                            {/* Show job-specific metadata */}
                                            {job.meta && (
                                              <div className="space-y-0.5 pl-2 border-l-2 border-gray-300">
                                                {/* open_conversation_job metadata */}
                                                {job.job_type === 'open_conversation_job' && (
                                                  <>
                                                    {job.meta.word_count !== undefined && (
                                                      <div>Words: <span className="font-medium">{job.meta.word_count}</span></div>
                                                    )}
                                                    {job.meta.speakers && job.meta.speakers.length > 0 && (
                                                      <div>Speakers: <span className="font-medium">{job.meta.speakers.join(', ')}</span></div>
                                                    )}
                                                    {job.meta.inactivity_seconds !== undefined && (
                                                      <div>Idle: <span className="font-medium">{Math.floor(job.meta.inactivity_seconds)}s</span></div>
                                                    )}
                                                    {job.meta.transcript && (
                                                      <div className="italic text-gray-500 truncate max-w-md">
                                                        "{job.meta.transcript.substring(0, 80)}..."
                                                      </div>
                                                    )}
                                                  </>
                                                )}

                                                {/* transcribe_full_audio_job metadata */}
                                                {job.job_type === 'transcribe_full_audio_job' && job.result && (
                                                  <>
                                                    {job.result.transcript && (
                                                      <div>Transcript: <span className="font-medium">{job.result.transcript.length} chars</span></div>
                                                    )}
                                                    {job.result.processing_time_seconds && (
                                                      <div>Processing: <span className="font-medium">{job.result.processing_time_seconds.toFixed(1)}s</span></div>
                                                    )}
                                                  </>
                                                )}

                                                {/* recognise_speakers_job metadata */}
                                                {job.job_type === 'recognise_speakers_job' && job.result && (
                                                  <>
                                                    {job.result.identified_speakers && job.result.identified_speakers.length > 0 && (
                                                      <div>Identified: <span className="font-medium">{job.result.identified_speakers.join(', ')}</span></div>
                                                    )}
                                                    {job.result.segment_count && (
                                                      <div>Segments: <span className="font-medium">{job.result.segment_count}</span></div>
                                                    )}
                                                  </>
                                                )}

                                                {/* process_memory_job metadata */}
                                                {job.job_type === 'process_memory_job' && job.result && (
                                                  <>
                                                    {job.result.memories_created !== undefined && (
                                                      <div>Memories: <span className="font-medium">{job.result.memories_created} created</span></div>
                                                    )}
                                                    {job.result.processing_time_seconds && (
                                                      <div>Processing: <span className="font-medium">{job.result.processing_time_seconds.toFixed(1)}s</span></div>
                                                    )}
                                                  </>
                                                )}

                                                {/* Show conversation_id if present */}
                                                {job.meta.conversation_id && (
                                                  <div className="font-mono text-gray-500">
                                                    Conv: {job.meta.conversation_id.substring(0, 8)}...
                                                  </div>
                                                )}
                                              </div>
                                            )}
                                          </div>
                                        )}
                                      </div>
                                    ))}
                                  </div>
                                ) : (
                                  <div className="text-xs text-gray-500 italic">No jobs found for this conversation</div>
                                )}
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>

                      {/* Pagination Controls */}
                      {totalPages > 1 && (
                        <div className="flex items-center justify-between mt-4 pt-4 border-t border-gray-200">
                          <div className="text-xs text-gray-600">
                            Showing {startIndex + 1}-{Math.min(endIndex, totalConversations)} of {totalConversations} conversations
                          </div>
                          <div className="flex items-center space-x-2">
                            <button
                              onClick={() => setCompletedConvPage(Math.max(1, completedConvPage - 1))}
                              disabled={completedConvPage === 1}
                              className={`px-3 py-1 text-xs rounded ${
                                completedConvPage === 1
                                  ? 'bg-gray-100 text-gray-400 cursor-not-allowed'
                                  : 'bg-blue-500 text-white hover:bg-blue-600'
                              }`}
                            >
                              Previous
                            </button>
                            <span className="text-xs text-gray-600">
                              Page {completedConvPage} of {totalPages}
                            </span>
                            <button
                              onClick={() => setCompletedConvPage(Math.min(totalPages, completedConvPage + 1))}
                              disabled={completedConvPage === totalPages}
                              className={`px-3 py-1 text-xs rounded ${
                                completedConvPage === totalPages
                                  ? 'bg-gray-100 text-gray-400 cursor-not-allowed'
                                  : 'bg-blue-500 text-white hover:bg-blue-600'
                              }`}
                            >
                              Next
                            </button>
                          </div>
                        </div>
                      )}
                    </>
                  );
                })()}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Events */}
      <div className="bg-white rounded-lg border overflow-hidden">
        <div
          className="px-6 py-4 border-b border-gray-200 flex justify-between items-center cursor-pointer"
          onClick={() => {
            const next = !eventsExpanded;
            setEventsExpanded(next);
            localStorage.setItem('queue_events_expanded', String(next));
          }}
        >
          <div className="flex items-center space-x-2">
            <Zap className="w-5 h-5 text-purple-600" />
            <h3 className="text-lg font-medium">Events</h3>
            <span className="text-xs text-gray-500">
              {(() => {
                const includes = Object.entries(eventFilters).filter(([, v]) => v === 'include').map(([k]) => k);
                const excludes = Object.entries(eventFilters).filter(([, v]) => v === 'exclude').map(([k]) => k);
                const hasFilters = includes.length > 0 || excludes.length > 0;
                if (!hasFilters) return `(${events.length})`;
                const count = includes.length > 0
                  ? events.filter(e => includes.includes(e.event) && !excludes.includes(e.event)).length
                  : events.filter(e => !excludes.includes(e.event)).length;
                return `(${count} / ${events.length})`;
              })()}
            </span>
          </div>
          <div className="flex items-center space-x-3">
            {eventsExpanded && events.length > 0 && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  queueApi.clearEvents().then(() => {
                    setEvents([]);
                    setEventFilters({});
                  });
                }}
                className="inline-flex items-center px-2 py-1 rounded text-xs text-red-500 hover:text-red-700 hover:bg-red-50 transition-colors"
                title="Clear all events"
              >
                <Trash2 className="w-3.5 h-3.5 mr-1" />
                Clear
              </button>
            )}
            {eventsExpanded ? <ChevronDown className="w-4 h-4 text-gray-500" /> : <ChevronRight className="w-4 h-4 text-gray-500" />}
          </div>
        </div>

        {eventsExpanded && [...new Set(events.map(e => e.event))].sort().length > 0 && (
          <div className="px-6 py-2 border-b border-gray-100 flex flex-wrap items-center gap-2">
            {[...new Set(events.map(e => e.event))].sort().map(eventType => {
              const state = eventFilters[eventType];
              return (
                <button
                  key={eventType}
                  onClick={() => cycleEventFilter(eventType)}
                  className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium border cursor-pointer transition-colors ${
                    state === 'include'
                      ? 'bg-blue-100 text-blue-700 border-blue-400'
                      : state === 'exclude'
                      ? 'bg-red-100 text-red-700 border-red-400 line-through'
                      : 'bg-gray-100 text-gray-500 border-gray-300'
                  }`}
                >
                  {eventType}
                </button>
              );
            })}
            {Object.keys(eventFilters).length > 0 && (
              <button
                onClick={() => setEventFilters({})}
                className="inline-flex items-center px-2 py-0.5 rounded text-xs text-gray-400 hover:text-gray-600 transition-colors"
              >
                Clear
              </button>
            )}
          </div>
        )}

        {eventsExpanded && (
          <div className="overflow-x-auto">
            {(() => {
              const includes = Object.entries(eventFilters).filter(([, v]) => v === 'include').map(([k]) => k);
              const excludes = Object.entries(eventFilters).filter(([, v]) => v === 'exclude').map(([k]) => k);
              let filtered = events;
              if (includes.length > 0) {
                filtered = filtered.filter(e => includes.includes(e.event));
              }
              filtered = filtered.filter(e => !excludes.includes(e.event));

              if (filtered.length === 0) {
                return (
                  <div className="px-6 py-8 text-center text-gray-500 text-sm">
                    No events recorded yet. Events are logged when system actions like conversation.complete, memory.processed, or button presses occur.
                  </div>
                );
              }

              return (
                <table className="min-w-full divide-y divide-gray-200">
                  <thead className="bg-gray-50">
                    <tr>
                      <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Time</th>
                      <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Event</th>
                      <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">User</th>
                      <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Plugins Triggered</th>
                      <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200">
                    {filtered.map((evt, idx) => {
                      const allSuccess = evt.plugins_executed.length > 0 && evt.plugins_executed.every(p => p.success);
                      const anyFailure = evt.plugins_executed.some(p => !p.success);

                      return (
                        <tr key={idx} className="hover:bg-gray-50">
                          <td className="px-4 py-2 text-xs text-gray-600 whitespace-nowrap">
                            {new Date(evt.timestamp * 1000).toLocaleTimeString()}
                          </td>
                          <td className="px-4 py-2">
                            <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${getEventColor(evt.event)}`}>
                              {evt.event}
                            </span>
                          </td>
                          <td className="px-4 py-2 text-xs text-gray-600 font-mono">
                            {evt.user_id.length > 12 ? `${evt.user_id.slice(-8)}` : evt.user_id}
                          </td>
                          <td className="px-4 py-2 text-xs text-gray-700">
                            {evt.plugins_executed.length > 0
                              ? evt.plugins_executed.map(p => p.plugin_id).join(', ')
                              : <span className="text-gray-400">none</span>
                            }
                          </td>
                          <td className="px-4 py-2">
                            <div className="flex items-center space-x-2">
                              {evt.plugins_executed.length === 0 ? (
                                <span className="text-xs text-gray-400">no plugins ran</span>
                              ) : allSuccess ? (
                                <span className="flex items-center space-x-1 text-xs text-green-600">
                                  <CheckCircle className="w-3.5 h-3.5" />
                                  <span>OK</span>
                                </span>
                              ) : anyFailure ? (
                                <span className="flex items-center space-x-1 text-xs text-red-600">
                                  <XCircle className="w-3.5 h-3.5" />
                                  <span>Error</span>
                                </span>
                              ) : (
                                <span className="text-xs text-gray-500">partial</span>
                              )}
                              {evt.plugins_executed.length > 0 && (
                                <button
                                  onClick={() => setSelectedEvent(evt)}
                                  className="text-gray-400 hover:text-gray-600 p-0.5 rounded hover:bg-gray-100"
                                  title="View details"
                                >
                                  <Eye className="w-3.5 h-3.5" />
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              );
            })()}
          </div>
        )}
      </div>

      {/* Filters */}
      <div className="bg-white rounded-lg border p-4">
        <h3 className="text-lg font-medium mb-4">Filters</h3>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Status</label>
            <select
              value={filters.status}
              onChange={(e) => setFilters({ ...filters, status: e.target.value })}
              className="w-full border border-gray-300 rounded-md px-3 py-2"
            >
              <option value="">All Statuses</option>
              <option value="queued">Queued</option>
              <option value="started">Started</option>
              <option value="finished">Finished</option>
              <option value="failed">Failed</option>
              <option value="canceled">Canceled</option>
              <option value="deferred">Deferred</option>
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Job Type</label>
            <select
              value={filters.job_type}
              onChange={(e) => setFilters({ ...filters, job_type: e.target.value })}
              className="w-full border border-gray-300 rounded-md px-3 py-2"
            >
              <option value="">All Types</option>
              <option value="process_audio_files">Audio File Processing</option>
              <option value="process_single_audio_file">Single Audio File</option>
              <option value="reprocess_transcript">Reprocess Transcript</option>
              <option value="reprocess_memory">Reprocess Memory</option>
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Priority</label>
            <select
              value={filters.priority}
              onChange={(e) => setFilters({ ...filters, priority: e.target.value })}
              className="w-full border border-gray-300 rounded-md px-3 py-2"
            >
              <option value="">All Priorities</option>
              <option value="high">High</option>
              <option value="normal">Normal</option>
              <option value="low">Low</option>
            </select>
          </div>

          <div className="flex items-end space-x-2">
            <button
              onClick={applyFilters}
              className="flex items-center space-x-1 px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700"
            >
              <Filter className="w-4 h-4" />
              <span>Apply</span>
            </button>
            <button
              onClick={clearFilters}
              className="flex items-center space-x-1 px-4 py-2 bg-gray-600 text-white rounded-md hover:bg-gray-700"
            >
              <X className="w-4 h-4" />
              <span>Clear</span>
            </button>
          </div>
        </div>
      </div>

          {/* Jobs Table */}
      <div className="bg-white rounded-lg border overflow-hidden">
        <div className="px-6 py-4 border-b border-gray-200 flex justify-between items-center">
          <h3 className="text-lg font-medium">Jobs</h3>
          {jobs.length > 0 && (
            <button
              onClick={() => {
                queueApi.clearJobs().then(() => {
                  fetchData();
                });
              }}
              className="inline-flex items-center px-2 py-1 rounded text-xs text-red-500 hover:text-red-700 hover:bg-red-50 transition-colors"
              title="Clear finished and failed jobs"
            >
              <Trash2 className="w-3.5 h-3.5 mr-1" />
              Clear
            </button>
          )}
        </div>

        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">Date</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">Conversation ID</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">Job ID</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">Type</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">Status</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">Duration</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase whitespace-nowrap">Actions</th>
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-gray-200">
              {jobs
                .filter((job) => {
                  if (filters.status && job.status !== filters.status) return false;
                  if (filters.job_type && job.job_type !== filters.job_type) return false;
                  if (filters.priority && job.meta?.priority !== filters.priority) return false;
                  return true;
                })
                .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()).map((job) => (
                <tr key={job.job_id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 text-sm text-gray-500 whitespace-nowrap">
                    {new Date(job.created_at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                  </td>
                  <td className="px-4 py-3 max-w-xs">
                    <div className="text-xs font-mono text-gray-600 truncate" title={job.meta?.conversation_id || 'N/A'}>
                      {job.meta?.conversation_id ? job.meta.conversation_id.substring(0, 8) : '—'}
                    </div>
                  </td>
                  <td className="px-4 py-3 max-w-xs">
                    <div className="text-xs font-mono text-gray-900 truncate" title={job.job_id}>
                      {job.job_id}
                    </div>
                  </td>
                  <td className="px-4 py-3 whitespace-nowrap">
                    <div className="text-sm text-gray-900">{getJobTypeShort(job.job_type)}</div>
                  </td>
                  <td className="px-4 py-3 whitespace-nowrap">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${getStatusColor(job.status)}`}>
                      {getStatusIcon(job.status)}
                      <span className="ml-1">{job.status.charAt(0).toUpperCase() + job.status.slice(1)}</span>
                    </span>
                  </td>
                  <td className="px-4 py-3 whitespace-nowrap">
                    <div className="text-sm text-gray-700 font-mono">
                      {formatDuration(job)}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-sm font-medium whitespace-nowrap">
                    <div className="flex items-center gap-2">
                      {job.status === 'failed' && (
                        <button
                          onClick={() => retryJob(job.job_id)}
                          className="text-blue-600 hover:text-blue-900"
                          title="Retry job"
                        >
                          <RotateCcw className="w-4 h-4" />
                        </button>
                      )}
                      <button
                        onClick={() => viewJobDetails(job.job_id)}
                        className="text-indigo-600 hover:text-indigo-900"
                        disabled={loadingJobDetails}
                        title="View details"
                      >
                        <Eye className="w-4 h-4" />
                      </button>
                      {(job.status === 'queued' || job.status === 'started') && (
                        <button
                          onClick={() => cancelJob(job.job_id)}
                          className="text-red-600 hover:text-red-900"
                          title="Cancel job"
                        >
                          <StopCircle className="w-4 h-4" />
                        </button>
                      )}
                      {job.status === 'finished' && (
                        <button
                          onClick={() => cancelJob(job.job_id)}
                          className="text-gray-400 hover:text-gray-600"
                          title="Delete job"
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {pagination.total > pagination.limit && (
          <div className="bg-white px-4 py-3 border-t border-gray-200 flex items-center justify-between">
            <div className="text-sm text-gray-700">
              Showing {pagination.offset + 1} to {Math.min(pagination.offset + pagination.limit, pagination.total)} of {pagination.total} results
            </div>
            <div className="flex space-x-2">
              <button
                onClick={prevPage}
                disabled={pagination.offset === 0}
                className="px-4 py-2 border border-gray-300 text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-50"
              >
                Previous
              </button>
              <button
                onClick={nextPage}
                disabled={!pagination.has_more}
                className="px-4 py-2 border border-gray-300 text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-50"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </div>
      {/* Old Jobs Table and Pagination - Removed in favor of session-based view above */}

      {/* Job Details Modal */}
      {selectedJob && (
        <div className="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50">
          <div className="relative top-20 mx-auto p-5 border w-11/12 max-w-6xl shadow-lg rounded-md bg-white">
            <div className="flex justify-between items-center mb-4">
              <h3 className="text-lg font-medium text-gray-900">Job Details</h3>
              <button onClick={() => setSelectedJob(null)} className="text-gray-400 hover:text-gray-600">
                <X className="w-5 h-5" />
              </button>
            </div>

            {loadingJobDetails ? (
              <div className="flex items-center justify-center py-12">
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
              </div>
            ) : (
              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium text-gray-700">Job ID</label>
                    <p className="text-sm text-gray-900 font-mono">{selectedJob.job_id}</p>
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700">Status</label>
                    <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${getStatusColor(selectedJob.status)}`}>
                      {getStatusIcon(selectedJob.status)}
                      <span className="ml-1">{selectedJob.status.charAt(0).toUpperCase() + selectedJob.status.slice(1)}</span>
                    </span>
                  </div>
                  {selectedJob.description && (
                    <div className="col-span-2">
                      <label className="block text-sm font-medium text-gray-700">Description</label>
                      <p className="text-sm text-gray-900">{selectedJob.description}</p>
                    </div>
                  )}
                  {selectedJob.func_name && (
                    <div className="col-span-2">
                      <label className="block text-sm font-medium text-gray-700">Function Name</label>
                      <p className="text-sm text-gray-900 font-mono">{selectedJob.func_name}</p>
                    </div>
                  )}
                  <div>
                    <label className="block text-sm font-medium text-gray-700">Created</label>
                    <p className="text-sm text-gray-900">{selectedJob.created_at ? formatDate(selectedJob.created_at) : '-'}</p>
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700">Started</label>
                    <p className="text-sm text-gray-900">{selectedJob.started_at ? formatDate(selectedJob.started_at) : '-'}</p>
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700">Ended</label>
                    <p className="text-sm text-gray-900">{selectedJob.ended_at ? formatDate(selectedJob.ended_at) : '-'}</p>
                  </div>
                </div>

                {selectedJob.args && selectedJob.args.length > 0 && (
                  <div>
                    <label className="block text-sm font-medium text-gray-700">Arguments</label>
                    <pre className="text-xs text-gray-900 bg-gray-50 p-2 rounded overflow-auto max-h-64 whitespace-pre-wrap break-words">
                      {JSON.stringify(selectedJob.args, null, 2)}
                    </pre>
                  </div>
                )}

                {selectedJob.kwargs && Object.keys(selectedJob.kwargs).length > 0 && (
                  <div>
                    <label className="block text-sm font-medium text-gray-700">Keyword Arguments</label>
                    <pre className="text-xs text-gray-900 bg-gray-50 p-2 rounded overflow-auto max-h-64 whitespace-pre-wrap break-words">
                      {JSON.stringify(selectedJob.kwargs, null, 2)}
                    </pre>
                  </div>
                )}

                {selectedJob.error_message && (
                  <div>
                    <label className="block text-sm font-medium text-gray-700">Error</label>
                    <pre className="text-xs text-red-600 bg-red-50 p-2 rounded overflow-auto max-h-64 whitespace-pre-wrap break-words">
                      {selectedJob.error_message}
                    </pre>
                  </div>
                )}

                {selectedJob.result && (
                  <div>
                    <label className="block text-sm font-medium text-gray-700">Result</label>
                    <pre className="text-xs text-gray-900 bg-green-50 p-2 rounded overflow-auto max-h-64 whitespace-pre-wrap break-words">
                      {JSON.stringify(selectedJob.result, null, 2)}
                    </pre>
                  </div>
                )}

                {/* Formatted Job Metadata - Job-specific displays */}
                {selectedJob.meta && Object.keys(selectedJob.meta).length > 0 && (
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-2">Job Metadata</label>

                    {/* open_conversation_job formatted metadata */}
                    {selectedJob.func_name?.includes('open_conversation_job') && (
                      <div className="bg-blue-50 p-3 rounded mb-3 space-y-2">
                        {selectedJob.meta.word_count !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Word Count:</span> {selectedJob.meta.word_count}
                          </div>
                        )}
                        {selectedJob.meta.speakers && selectedJob.meta.speakers.length > 0 && (
                          <div className="text-sm">
                            <span className="font-medium">Speakers:</span> {selectedJob.meta.speakers.join(', ')}
                          </div>
                        )}
                        {selectedJob.meta.transcript_length !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Transcript Length:</span> {selectedJob.meta.transcript_length} chars
                          </div>
                        )}
                        {selectedJob.meta.duration_seconds !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Duration:</span> {selectedJob.meta.duration_seconds.toFixed(1)}s
                          </div>
                        )}
                        {selectedJob.meta.inactivity_seconds !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Idle Time:</span> {Math.floor(selectedJob.meta.inactivity_seconds)}s
                          </div>
                        )}
                        {selectedJob.meta.chunks_processed !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Chunks Processed:</span> {selectedJob.meta.chunks_processed}
                          </div>
                        )}
                        {selectedJob.meta.transcript && (
                          <div className="mt-2">
                            <div className="text-sm font-medium mb-1">Transcript:</div>
                            <div className="text-sm italic text-gray-700 bg-white p-2 rounded border border-gray-200 max-h-32 overflow-y-auto">
                              "{selectedJob.meta.transcript}"
                            </div>
                          </div>
                        )}
                      </div>
                    )}

                    {/* process_memory_job formatted metadata */}
                    {selectedJob.func_name?.includes('process_memory_job') && selectedJob.meta.memory_details && selectedJob.meta.memory_details.length > 0 && (
                      <div className="bg-pink-50 p-3 rounded mb-3 space-y-2">
                        <div className="text-sm">
                          <span className="font-medium">Memories Created:</span> {selectedJob.meta.memories_created || selectedJob.meta.memory_details.length}
                        </div>
                        {selectedJob.meta.processing_time !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Processing Time:</span> {selectedJob.meta.processing_time.toFixed(1)}s
                          </div>
                        )}
                        <div className="mt-2">
                          <div className="text-sm font-medium mb-1">Memory Details:</div>
                          <div className="space-y-1">
                            {selectedJob.meta.memory_details.map((mem: any, idx: number) => (
                              <div key={idx} className="text-xs bg-pink-100 p-2 rounded border border-pink-200">
                                {mem.text}
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    )}

                    {/* stream_speech_detection_job formatted metadata */}
                    {selectedJob.func_name?.includes('stream_speech_detection_job') && (
                      <div className="bg-yellow-50 p-3 rounded mb-3 space-y-2">
                        {selectedJob.meta.speech_detected_at && (
                          <div className="text-sm">
                            <span className="font-medium">Speech Detected At:</span> {new Date(selectedJob.meta.speech_detected_at).toLocaleString()}
                          </div>
                        )}
                        {selectedJob.meta.detected_speakers && selectedJob.meta.detected_speakers.length > 0 && (
                          <div className="text-sm">
                            <span className="font-medium">Detected Speakers:</span> {selectedJob.meta.detected_speakers.join(', ')}
                          </div>
                        )}
                        {selectedJob.meta.conversation_job_id && (
                          <div className="text-sm">
                            <span className="font-medium">Conversation Job:</span> {selectedJob.meta.conversation_job_id}
                          </div>
                        )}
                      </div>
                    )}

                    {/* transcribe_full_audio_job formatted metadata */}
                    {selectedJob.func_name?.includes('transcribe_full_audio_job') && (selectedJob.meta.title || selectedJob.meta.summary) && (
                      <div className="bg-purple-50 p-3 rounded mb-3 space-y-2">
                        {selectedJob.meta.title && (
                          <div className="text-sm">
                            <span className="font-medium">Title:</span> {selectedJob.meta.title}
                          </div>
                        )}
                        {selectedJob.meta.summary && (
                          <div className="text-sm">
                            <span className="font-medium">Summary:</span> {selectedJob.meta.summary}
                          </div>
                        )}
                        {selectedJob.meta.transcript_length !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Transcript Length:</span> {selectedJob.meta.transcript_length} chars
                          </div>
                        )}
                        {selectedJob.meta.word_count !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Word Count:</span> {selectedJob.meta.word_count}
                          </div>
                        )}
                        {selectedJob.meta.processing_time !== undefined && (
                          <div className="text-sm">
                            <span className="font-medium">Processing Time:</span> {selectedJob.meta.processing_time.toFixed(1)}s
                          </div>
                        )}
                      </div>
                    )}

                    {/* Raw JSON metadata (collapsible) */}
                    <details className="mt-2">
                      <summary className="text-sm font-medium text-gray-700 cursor-pointer hover:text-gray-900">
                        Raw Metadata JSON
                      </summary>
                      <pre className="text-xs text-gray-900 bg-blue-50 p-2 rounded overflow-auto max-h-64 mt-2 whitespace-pre-wrap break-words">
                        {JSON.stringify(selectedJob.meta, null, 2)}
                      </pre>
                    </details>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Event Detail Modal */}
      {selectedEvent && (
        <div className="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50">
          <div className="relative top-20 mx-auto p-5 border w-11/12 md:w-2/3 lg:w-1/2 shadow-lg rounded-md bg-white">
            <div className="flex justify-between items-center mb-4">
              <h3 className="text-lg font-medium text-gray-900">Event Details</h3>
              <button onClick={() => setSelectedEvent(null)} className="text-gray-400 hover:text-gray-600">
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700">Time</label>
                  <p className="text-sm text-gray-900">{new Date(selectedEvent.timestamp * 1000).toLocaleString()}</p>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700">Event</label>
                  <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${getEventColor(selectedEvent.event)}`}>
                    {selectedEvent.event}
                  </span>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700">User</label>
                  <p className="text-sm text-gray-900 font-mono">{selectedEvent.user_id}</p>
                </div>
                {selectedEvent.metadata?.client_id && (
                  <div>
                    <label className="block text-sm font-medium text-gray-700">Client</label>
                    <p className="text-sm text-gray-900 font-mono">{selectedEvent.metadata.client_id}</p>
                  </div>
                )}
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">Plugin Results</label>
                <div className="space-y-2">
                  {selectedEvent.plugins_executed.map((p, i) => (
                    <div
                      key={i}
                      className={`p-3 rounded-lg border ${p.success ? 'bg-green-50 border-green-200' : 'bg-red-50 border-red-200'}`}
                    >
                      <div className="flex items-center space-x-2 mb-1">
                        {p.success
                          ? <CheckCircle className="w-4 h-4 text-green-600" />
                          : <XCircle className="w-4 h-4 text-red-600" />
                        }
                        <span className="text-sm font-medium">{p.plugin_id}</span>
                        <span className={`text-xs px-1.5 py-0.5 rounded ${p.success ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                          {p.success ? 'OK' : 'Error'}
                        </span>
                      </div>
                      {p.message && (
                        <p className={`text-sm ml-6 ${p.success ? 'text-green-800' : 'text-red-800'}`}>
                          {p.message}
                        </p>
                      )}
                    </div>
                  ))}
                </div>
              </div>

              {Object.keys(selectedEvent.metadata).length > 0 && (
                <details>
                  <summary className="text-sm font-medium text-gray-700 cursor-pointer hover:text-gray-900">
                    Raw Metadata
                  </summary>
                  <pre className="text-xs text-gray-900 bg-gray-50 p-2 rounded overflow-auto max-h-40 mt-2 whitespace-pre-wrap break-words">
                    {JSON.stringify(selectedEvent.metadata, null, 2)}
                  </pre>
                </details>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Flush Jobs Modal */}
      {showFlushModal && (
        <div className="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50">
          <div className="relative top-20 mx-auto p-5 border w-11/12 md:w-1/2 lg:w-1/3 shadow-lg rounded-md bg-white">
            <div className="flex justify-between items-center mb-4">
              <h3 className="text-lg font-medium text-gray-900 flex items-center">
                <Trash2 className="w-5 h-5 mr-2" />
                Flush Jobs
              </h3>
              <button onClick={() => setShowFlushModal(false)} className="text-gray-400 hover:text-gray-600">
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="space-y-4">
              <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-3">
                <div className="flex items-center">
                  <AlertTriangle className="w-5 h-5 text-yellow-600 mr-2" />
                  <span className="text-sm text-yellow-800">This will permanently remove jobs from the database</span>
                </div>
              </div>

              <div className="space-y-3">
                <div>
                  <label className="flex items-center space-x-2">
                    <input
                      type="radio"
                      name="flushType"
                      checked={!flushSettings.flush_all}
                      onChange={() => setFlushSettings(prev => ({ ...prev, flush_all: false }))}
                      className="text-blue-600"
                    />
                    <span className="text-sm font-medium">Flush old inactive jobs (recommended)</span>
                  </label>

                  {!flushSettings.flush_all && (
                    <div className="ml-6 mt-2 space-y-2">
                      <div>
                        <label className="block text-xs text-gray-600 mb-1">Remove jobs older than:</label>
                        <select
                          value={flushSettings.older_than_hours}
                          onChange={(e) => setFlushSettings(prev => ({ ...prev, older_than_hours: parseInt(e.target.value) }))}
                          className="w-full text-sm border border-gray-300 rounded px-2 py-1"
                        >
                          <option value={1}>1 hour</option>
                          <option value={6}>6 hours</option>
                          <option value={12}>12 hours</option>
                          <option value={24}>24 hours</option>
                          <option value={72}>3 days</option>
                          <option value={168}>1 week</option>
                        </select>
                      </div>

                      <div>
                        <label className="block text-xs text-gray-600 mb-1">Job statuses to remove:</label>
                        <div className="space-y-1">
                          {['finished', 'failed', 'canceled'].map(status => (
                            <label key={status} className="flex items-center space-x-2">
                              <input
                                type="checkbox"
                                checked={flushSettings.statuses.includes(status)}
                                onChange={(e) => {
                                  if (e.target.checked) {
                                    setFlushSettings(prev => ({
                                      ...prev,
                                      statuses: [...prev.statuses, status]
                                    }));
                                  } else {
                                    setFlushSettings(prev => ({
                                      ...prev,
                                      statuses: prev.statuses.filter(s => s !== status)
                                    }));
                                  }
                                }}
                                className="text-blue-600"
                              />
                              <span className="text-xs capitalize">{status}</span>
                            </label>
                          ))}
                        </div>
                      </div>
                    </div>
                  )}
                </div>

                <div>
                  <label className="flex items-center space-x-2">
                    <input
                      type="radio"
                      name="flushType"
                      checked={flushSettings.flush_all}
                      onChange={() => setFlushSettings(prev => ({ ...prev, flush_all: true }))}
                      className="text-red-600"
                    />
                    <span className="text-sm font-medium text-red-600">Flush ALL jobs (DANGER!)</span>
                  </label>

                  {flushSettings.flush_all && (
                    <div className="ml-6 mt-2 space-y-2">
                      <div className="bg-red-50 border border-red-200 rounded p-2">
                        <p className="text-xs text-red-800">
                          ⚠️ This will flush queued, started, deferred, scheduled, and canceled jobs.
                          {!flushSettings.include_failed && !flushSettings.include_completed &&
                            " Failed and completed jobs preserved for debugging."}
                        </p>
                      </div>

                      <div className="space-y-1">
                        <label className="flex items-center space-x-2">
                          <input
                            type="checkbox"
                            checked={flushSettings.include_failed}
                            onChange={(e) => setFlushSettings(prev => ({ ...prev, include_failed: e.target.checked }))}
                            className="text-red-600"
                          />
                          <span className="text-xs text-gray-700">Also flush failed jobs</span>
                        </label>

                        <label className="flex items-center space-x-2">
                          <input
                            type="checkbox"
                            checked={flushSettings.include_completed}
                            onChange={(e) => setFlushSettings(prev => ({ ...prev, include_completed: e.target.checked }))}
                            className="text-red-600"
                          />
                          <span className="text-xs text-gray-700">Also flush completed jobs</span>
                        </label>
                      </div>
                    </div>
                  )}
                </div>
              </div>

              <div className="flex space-x-2 pt-4 border-t">
                <button
                  onClick={() => setShowFlushModal(false)}
                  className="flex-1 px-4 py-2 border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50"
                >
                  Cancel
                </button>
                <button
                  onClick={flushJobs}
                  disabled={flushing || (!flushSettings.flush_all && flushSettings.statuses.length === 0)}
                  className={`flex-1 px-4 py-2 text-white rounded-lg disabled:opacity-50 ${
                    flushSettings.flush_all
                      ? 'bg-red-600 hover:bg-red-700'
                      : 'bg-blue-600 hover:bg-blue-700'
                  }`}
                >
                  {flushing ? (
                    <>
                      <RotateCcw className="w-4 h-4 animate-spin inline mr-2" />
                      Flushing...
                    </>
                  ) : (
                    <>
                      <Trash2 className="w-4 h-4 inline mr-2" />
                      {flushSettings.flush_all ? 'Flush ALL Jobs' : 'Flush Selected Jobs'}
                    </>
                  )}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default Queue;