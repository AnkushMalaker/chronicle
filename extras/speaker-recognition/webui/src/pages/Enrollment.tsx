import { useState, useRef, useCallback, useEffect } from 'react'
import { Mic, MicOff, Play, Pause, Save, Trash2, CheckCircle, AlertCircle } from 'lucide-react'
import { useUser } from '../contexts/UserContext'
import {
  loadAudioBuffer,
  createAudioContext,
  decodeAudioData,
  extractAudioSamples,
  calculateSNR,
  formatDuration,
  convertBlobToWav
} from '../utils/audioUtils'
import { newSpeakerId } from '../utils/common'
import { apiService } from '../services/api'
import FileUploader from '../components/FileUploader'

interface EnrollmentSession {
  id: string
  speakerName: string
  audioFiles: EnrollmentAudio[]
  quality: 'excellent' | 'good' | 'fair' | 'poor'
  totalDuration: number
  status: 'draft' | 'processing' | 'completed' | 'failed'
}

interface EnrollmentAudio {
  id: string
  name: string
  blob: Blob
  duration: number
  snr: number
  quality: 'excellent' | 'good' | 'fair' | 'poor'
  source: 'upload' | 'recording'
}

export default function Enrollment() {
  const { user } = useUser()
  const [sessions, setSessions] = useState<EnrollmentSession[]>([])
  const [currentSession, setCurrentSession] = useState<EnrollmentSession | null>(null)
  const [isRecording, setIsRecording] = useState(false)
  const [recordingTime, setRecordingTime] = useState(0)
  const [playingAudioId, setPlayingAudioId] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const audioChunksRef = useRef<Blob[]>([])
  const recordingIntervalRef = useRef<NodeJS.Timeout | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const audioSourceRef = useRef<AudioBufferSourceNode | null>(null)

  // Cleanup on component unmount
  useEffect(() => {
    return () => {
      // Stop recording if active
      if (mediaRecorderRef.current) {
        mediaRecorderRef.current.stop()
      }

      // Clear interval
      if (recordingIntervalRef.current) {
        clearInterval(recordingIntervalRef.current)
      }

      // Stop audio playback
      if (audioSourceRef.current) {
        audioSourceRef.current.stop()
      }

      // Close audio context
      if (audioContextRef.current && audioContextRef.current.state !== 'closed') {
        audioContextRef.current.close()
      }
    }
  }, [])

  const createNewSession = useCallback((speakerName: string) => {
    const newSession: EnrollmentSession = {
      id: Math.random().toString(36),
      speakerName,
      audioFiles: [],
      quality: 'poor',
      totalDuration: 0,
      status: 'draft'
    }
    setSessions(prev => [...prev, newSession])
    setCurrentSession(newSession)
  }, [])

  const calculateSessionQuality = useCallback((audioFiles: EnrollmentAudio[]): 'excellent' | 'good' | 'fair' | 'poor' => {
    if (audioFiles.length === 0) return 'poor'

    const totalDurationMs = audioFiles.reduce((sum, audio) => sum + audio.duration, 0)
    const totalDurationSeconds = totalDurationMs / 1000 // Convert to seconds for quality thresholds
    const avgSNR = audioFiles.reduce((sum, audio) => sum + audio.snr, 0) / audioFiles.length

    if (totalDurationSeconds >= 60 && avgSNR >= 30 && audioFiles.length >= 5) return 'excellent'
    if (totalDurationSeconds >= 30 && avgSNR >= 20 && audioFiles.length >= 3) return 'good'
    if (totalDurationSeconds >= 15 && avgSNR >= 15 && audioFiles.length >= 2) return 'fair'
    return 'poor'
  }, [])

  const updateSession = useCallback((sessionId: string, updates: Partial<EnrollmentSession>) => {
    setSessions(prev => prev.map(session => {
      if (session.id === sessionId) {
        const updatedSession = { ...session, ...updates }
        if (updates.audioFiles) {
          updatedSession.totalDuration = updates.audioFiles.reduce((sum, audio) => sum + audio.duration, 0)
          updatedSession.quality = calculateSessionQuality(updates.audioFiles)
        }
        return updatedSession
      }
      return session
    }))

    if (currentSession?.id === sessionId) {
      setCurrentSession(prev => {
        if (!prev) return null
        const updatedSession = { ...prev, ...updates }
        if (updates.audioFiles) {
          updatedSession.totalDuration = updates.audioFiles.reduce((sum, audio) => sum + audio.duration, 0)
          updatedSession.quality = calculateSessionQuality(updates.audioFiles)
        }
        return updatedSession
      })
    }
  }, [currentSession, calculateSessionQuality])

  const startRecording = useCallback(async () => {
    if (!currentSession) {
      alert('Please create an enrollment session first.')
      return
    }

    try {
      // Check if HTTPS is required
      if (location.protocol !== 'https:' && location.hostname !== 'localhost') {
        alert('Microphone access requires HTTPS. Please use HTTPS or localhost.')
        return
      }

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
          // Removed sampleRate constraint for better compatibility
        }
      })

      // Try WAV first, fallback to WebM if not supported
      let mimeType = 'audio/wav'
      if (!MediaRecorder.isTypeSupported(mimeType)) {
        mimeType = 'audio/webm'
        if (!MediaRecorder.isTypeSupported(mimeType)) {
          mimeType = '' // Let browser choose
        }
      }

      const mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)

      audioChunksRef.current = []

      mediaRecorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          audioChunksRef.current.push(event.data)
        }
      }

      mediaRecorder.onstop = async () => {
        const blob = new Blob(audioChunksRef.current, { type: mimeType || 'audio/webm' })
        await processRecording(blob)
        stream.getTracks().forEach(track => track.stop())
      }

      mediaRecorder.onerror = (event) => {
        console.error('MediaRecorder error:', event)
        alert('Recording failed. Please try again.')
        setIsRecording(false)
        stream.getTracks().forEach(track => track.stop())
      }

      mediaRecorderRef.current = mediaRecorder
      setIsRecording(true)
      setRecordingTime(0)

      // Start timer immediately (track milliseconds for formatDuration compatibility)
      recordingIntervalRef.current = setInterval(() => {
        setRecordingTime(prev => prev + 1000)
      }, 1000)

      mediaRecorder.start(250) // Increased interval for better stability

    } catch (error) {
      console.error('Failed to start recording:', error)

      // Clean up if recording failed
      setIsRecording(false)
      setRecordingTime(0)
      if (recordingIntervalRef.current) {
        clearInterval(recordingIntervalRef.current)
        recordingIntervalRef.current = null
      }

      const err = error as DOMException
      let errorMessage = 'Failed to access microphone. '
      if (err.name === 'NotAllowedError') {
        errorMessage += 'Please allow microphone access and try again.'
      } else if (err.name === 'NotFoundError') {
        errorMessage += 'No microphone found. Please check your device.'
      } else if (err.name === 'NotSupportedError') {
        errorMessage += 'Recording not supported in this browser.'
      } else {
        errorMessage += 'Please check permissions and try again.'
      }

      alert(errorMessage)
    }
  }, [currentSession])

  const stopRecording = useCallback(() => {
    if (mediaRecorderRef.current && isRecording) {
      try {
        if (mediaRecorderRef.current.state === 'recording') {
          mediaRecorderRef.current.stop()
        }
      } catch (error) {
        console.error('Error stopping MediaRecorder:', error)
      }

      setIsRecording(false)

      if (recordingIntervalRef.current) {
        clearInterval(recordingIntervalRef.current)
        recordingIntervalRef.current = null
      }
    }
  }, [isRecording])

  const processRecording = useCallback(async (blob: Blob) => {
    if (!currentSession) {
      console.error('No current session for processing recording')
      return
    }

    try {
      console.log('Processing recording blob:', blob.type, blob.size, 'bytes')

      // Convert WebM blob to WAV if needed for better backend compatibility
      let processedBlob = blob
      if (blob.type.includes('webm')) {
        console.log('Converting WebM recording to WAV format...')
        processedBlob = await convertBlobToWav(blob)
        console.log('Conversion successful:', processedBlob.type, processedBlob.size, 'bytes')
      }

      // Convert blob to audio buffer for analysis
      const arrayBuffer = await processedBlob.arrayBuffer()

      if (arrayBuffer.byteLength === 0) {
        alert('Recording is empty. Please try recording again.')
        return
      }

      const audioContext = createAudioContext()
      audioContextRef.current = audioContext

      console.log('Decoding audio data...')
      const audioBuffer = await decodeAudioData(audioContext, arrayBuffer)

      if (audioBuffer.duration === 0) {
        alert('Recording has no duration. Please try recording again.')
        return
      }

      console.log('Audio decoded successfully:', audioBuffer.duration, 'seconds')

      const samples = extractAudioSamples(audioBuffer)
      const snr = calculateSNR(samples)

      const quality = snr >= 30 ? 'excellent' : snr >= 20 ? 'good' : snr >= 15 ? 'fair' : 'poor'

      const newAudio: EnrollmentAudio = {
        id: Math.random().toString(36),
        name: `Recording ${new Date().toLocaleTimeString()}`,
        blob: processedBlob, // Use the converted blob for backend submission
        duration: audioBuffer.duration * 1000, // Convert seconds to milliseconds
        snr,
        quality,
        source: 'recording'
      }

      const updatedAudioFiles = [...currentSession.audioFiles, newAudio]
      updateSession(currentSession.id, { audioFiles: updatedAudioFiles })

      console.log('Recording processed successfully:', newAudio)

    } catch (error) {
      console.error('Failed to process recording:', error)

      const err = error as DOMException
      let errorMessage = 'Failed to process recording. '
      if (err.name === 'EncodingError' || err.message.includes('decode')) {
        errorMessage += 'Audio format not supported. Try using a different browser or check your microphone settings.'
      } else if (err.message.includes('context')) {
        errorMessage += 'Audio processing failed. Please try again.'
      } else if (err.message.includes('conversion')) {
        errorMessage += 'Audio conversion failed. Please try again or use a different browser.'
      } else {
        errorMessage += 'Please try again or refresh the page.'
      }

      alert(errorMessage)
    }
  }, [currentSession, updateSession])

  const handleFileUpload = useCallback(async (files: File[]) => {
    if (!currentSession) return

    for (const file of files) {
      if (!file.name.toLowerCase().endsWith('.wav')) {
        alert('Please select a WAV audio file. Other formats are not currently supported.')
        continue
      }

      try {
        const arrayBuffer = await loadAudioBuffer(file)
        const audioContext = createAudioContext()
        const audioBuffer = await decodeAudioData(audioContext, arrayBuffer)
        const samples = extractAudioSamples(audioBuffer)
        const snr = calculateSNR(samples)

        const quality = snr >= 30 ? 'excellent' : snr >= 20 ? 'good' : snr >= 15 ? 'fair' : 'poor'

        const newAudio: EnrollmentAudio = {
          id: Math.random().toString(36),
          name: file.name,
          blob: file,
          duration: audioBuffer.duration * 1000, // Convert seconds to milliseconds
          snr,
          quality,
          source: 'upload'
        }

        const updatedAudioFiles = [...currentSession.audioFiles, newAudio]
        updateSession(currentSession.id, { audioFiles: updatedAudioFiles })

      } catch (error) {
        console.error('Failed to process uploaded file:', error)
        alert(`Failed to process ${file.name}. Please try a different file.`)
      }
    }
  }, [currentSession, updateSession])

  const playAudio = useCallback(async (audio: EnrollmentAudio) => {
    try {
      // Stop any currently playing audio
      if (audioSourceRef.current) {
        audioSourceRef.current.stop()
        audioSourceRef.current = null
      }

      const arrayBuffer = await audio.blob.arrayBuffer()
      if (!audioContextRef.current) {
        audioContextRef.current = createAudioContext()
      }

      const audioBuffer = await decodeAudioData(audioContextRef.current, arrayBuffer)
      const source = audioContextRef.current.createBufferSource()
      source.buffer = audioBuffer
      source.connect(audioContextRef.current.destination)

      source.start(0)
      audioSourceRef.current = source
      setPlayingAudioId(audio.id)

      source.onended = () => {
        setPlayingAudioId(null)
        audioSourceRef.current = null
      }

    } catch (error) {
      console.error('Failed to play audio:', error)
      setPlayingAudioId(null)
    }
  }, [])

  const stopAudio = useCallback(() => {
    if (audioSourceRef.current) {
      audioSourceRef.current.stop()
      audioSourceRef.current = null
      setPlayingAudioId(null)
    }
  }, [])

  const removeAudio = useCallback((audioId: string) => {
    if (!currentSession) return

    const updatedAudioFiles = currentSession.audioFiles.filter(audio => audio.id !== audioId)
    updateSession(currentSession.id, { audioFiles: updatedAudioFiles })

    if (playingAudioId === audioId) {
      stopAudio()
    }
  }, [currentSession, playingAudioId, stopAudio, updateSession])

  const submitEnrollment = useCallback(async () => {
    if (!currentSession || !user) return

    if (currentSession.audioFiles.length === 0) {
      alert('Please add at least one audio sample before submitting.')
      return
    }

    if (currentSession.quality === 'poor') {
      const proceed = confirm(
        'The audio quality is poor. This may result in reduced speaker recognition accuracy. Continue anyway?'
      )
      if (!proceed) return
    }

    setIsSubmitting(true)
    updateSession(currentSession.id, { status: 'processing' })

    try {
      const formData = new FormData()

      const speakerId = newSpeakerId()

      // BatchEnrollRequest expects exactly these field names
      formData.append('speaker_name', currentSession.speakerName)
      formData.append('speaker_id', speakerId)
      formData.append('user_id', String(user.id))

      // Add all audio files (backend expects 'files' field for batch enrollment)
      for (const audio of currentSession.audioFiles) {
        formData.append('files', audio.blob, audio.name)
      }

      const response = await apiService.post('/enroll/batch', formData, {
        timeout: 120000, // 2 minutes for enrollment operations
      })

      updateSession(currentSession.id, { status: 'completed' })
      const message = response.data?.audio_saved
        ? `Speaker enrollment completed successfully! ${response.data.saved_files || 0} audio files saved.`
        : 'Speaker enrollment completed successfully!'
      alert(message)
      setCurrentSession(null)

    } catch (error) {
      console.error('Failed to submit enrollment:', error)
      updateSession(currentSession.id, { status: 'failed' })
      alert('Failed to submit enrollment. Please try again.')
    } finally {
      setIsSubmitting(false)
    }
  }, [currentSession, user, updateSession])

  const getQualityColor = useCallback((quality: string) => {
    switch (quality) {
      case 'excellent': return 'text-green-600 bg-green-100 dark:bg-green-900 dark:text-green-200'
      case 'good': return 'text-blue-600 bg-blue-100 dark:bg-blue-900 dark:text-blue-200'
      case 'fair': return 'text-yellow-600 bg-yellow-100 dark:bg-yellow-900 dark:text-yellow-200'
      default: return 'text-red-600 bg-red-100 dark:bg-red-900 dark:text-red-200'
    }
  }, [])

  const getQualityIcon = useCallback((quality: string) => {
    switch (quality) {
      case 'excellent':
      case 'good':
        return <CheckCircle className="h-4 w-4" />
      default:
        return <AlertCircle className="h-4 w-4" />
    }
  }, [])

  if (!user) {
    return (
      <div className="text-center py-12">
        <p className="text-muted">Please select a user to continue.</p>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="heading-lg">🎙️ Speaker Enrollment</h1>
        {!currentSession && (
          <button
            onClick={() => {
              const name = prompt('Enter speaker name:')
              if (name?.trim()) {
                createNewSession(name.trim())
              }
            }}
            className="px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 dark:bg-blue-700 dark:hover:bg-blue-800"
          >
            New Enrollment
          </button>
        )}
      </div>

      <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-4 dark:bg-blue-900 dark:border-blue-800">
        <p className="text-blue-800 text-sm dark:text-blue-200">
          <span className="font-medium">💾 Audio Storage:</span> All enrollment audio files are automatically saved for future reference and reprocessing.
        </p>
      </div>

      <p className="text-secondary">
        Enroll new speakers by uploading audio files or recording directly in your browser.
      </p>

      {/* Current Session */}
      {currentSession && (
        <div className="card p-6">
          <div className="flex items-center justify-between mb-6">
            <div>
              <h2 className="heading-md">{currentSession.speakerName}</h2>
              <div className="flex items-center space-x-4 mt-2">
                <span className={`inline-flex items-center space-x-1 px-2 py-1 rounded-full text-xs font-medium ${getQualityColor(currentSession.quality)}`}>
                  {getQualityIcon(currentSession.quality)}
                  <span>{currentSession.quality.charAt(0).toUpperCase() + currentSession.quality.slice(1)}</span>
                </span>
                <span className="text-sm text-muted">
                  {currentSession.audioFiles.length} samples • {formatDuration(currentSession.totalDuration)}
                </span>
              </div>
            </div>
            <div className="flex space-x-2">
              <button
                onClick={() => setCurrentSession(null)}
                className="px-3 py-1 text-secondary hover:text-primary"
              >
                Cancel
              </button>
              <button
                onClick={submitEnrollment}
                disabled={isSubmitting || currentSession.audioFiles.length === 0}
                className="flex items-center space-x-2 px-4 py-2 bg-green-600 text-white rounded-md hover:bg-green-700 disabled:opacity-50 dark:bg-green-700 dark:hover:bg-green-800"
              >
                <Save className="h-4 w-4" />
                <span>{isSubmitting ? 'Submitting...' : 'Submit Enrollment'}</span>
              </button>
            </div>
          </div>

          {/* Recording Section */}
          <div className="grid md:grid-cols-2 gap-6 mb-6">
            <div className="card p-4">
              <h3 className="heading-sm mb-4">🎤 Record Audio</h3>
              <div className="text-center space-y-4">
                {!isRecording ? (
                  <button
                    onClick={startRecording}
                    className="flex items-center space-x-2 px-6 py-3 bg-red-600 text-white rounded-md hover:bg-red-700 mx-auto dark:bg-red-700 dark:hover:bg-red-800"
                  >
                    <Mic className="h-5 w-5" />
                    <span>Start Recording</span>
                  </button>
                ) : (
                  <div className="space-y-3">
                    <div className="flex items-center justify-center space-x-2 text-red-600 dark:text-red-400">
                      <div className="w-3 h-3 bg-red-600 rounded-full animate-pulse"></div>
                      <span className="font-medium">Recording... {formatDuration(recordingTime)}</span>
                    </div>
                    <button
                      onClick={stopRecording}
                      className="flex items-center space-x-2 px-6 py-3 bg-gray-600 text-white rounded-md hover:bg-gray-700 mx-auto dark:bg-gray-700 dark:hover:bg-gray-800"
                    >
                      <MicOff className="h-5 w-5" />
                      <span>Stop Recording</span>
                    </button>
                  </div>
                )}
                <div className="text-sm text-muted space-y-1">
                  <p>Speak clearly for 10-30 seconds</p>
                  <p className="text-xs">
                    {location.protocol !== 'https:' && location.hostname !== 'localhost'
                      ? '⚠️ HTTPS required for microphone access'
                      : '✓ Ready to record'}
                  </p>
                </div>
              </div>
            </div>

            {/* File Upload Section */}
            <div className="card p-4">
              <h3 className="heading-sm mb-4">📁 Upload Audio</h3>
              <FileUploader
                onUpload={handleFileUpload}
                accept=".wav"
                multiple={true}
              />
            </div>
          </div>

          {/* Audio Samples List */}
          {currentSession.audioFiles.length > 0 && (
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <h3 className="heading-sm">Audio Samples</h3>
                <span className="text-sm text-green-600 bg-green-100 px-2 py-1 rounded-full dark:bg-green-900 dark:text-green-200">
                  💾 Files will be saved during enrollment
                </span>
              </div>
              <div className="space-y-3">
                {currentSession.audioFiles.map((audio) => (
                  <div
                    key={audio.id}
                    className="flex items-center justify-between p-4 card-secondary rounded-lg"
                  >
                    <div className="flex items-center space-x-4">
                      <div>
                        <p className="font-medium text-primary">{audio.name}</p>
                        <div className="flex items-center space-x-3 text-sm text-muted">
                          <span>{formatDuration(audio.duration)}</span>
                          <span>SNR: {audio.snr.toFixed(1)} dB</span>
                          <span className={`inline-flex items-center space-x-1 px-2 py-1 rounded-full text-xs ${getQualityColor(audio.quality)}`}>
                            {getQualityIcon(audio.quality)}
                            <span>{audio.quality}</span>
                          </span>
                          <span className="text-blue-600 dark:text-blue-400">({audio.source})</span>
                          <span className="text-green-600 dark:text-green-400">• will be saved</span>
                        </div>
                      </div>
                    </div>
                    <div className="flex space-x-2">
                      <button
                        onClick={() => playingAudioId === audio.id ? stopAudio() : playAudio(audio)}
                        className={`p-2 rounded ${
                          playingAudioId === audio.id
                            ? 'bg-red-100 text-red-700 hover:bg-red-200 dark:bg-red-900 dark:text-red-200 dark:hover:bg-red-800'
                            : 'bg-blue-100 text-blue-700 hover:bg-blue-200 dark:bg-blue-900 dark:text-blue-200 dark:hover:bg-blue-800'
                        }`}
                      >
                        {playingAudioId === audio.id ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
                      </button>
                      <button
                        onClick={() => removeAudio(audio.id)}
                        className="p-2 bg-red-100 text-red-700 rounded hover:bg-red-200 dark:bg-red-900 dark:text-red-200 dark:hover:bg-red-800"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}


          {/* Quality Guidelines */}
          <div className="mt-6 p-4 bg-blue-50 border border-blue-200 rounded-lg dark:bg-blue-900 dark:border-blue-800">
            <h4 className="font-medium text-blue-800 dark:text-blue-200 mb-2">Quality Guidelines</h4>
            <ul className="text-sm text-blue-700 dark:text-blue-300 space-y-1">
              <li>• Excellent: 60+ seconds, 5+ samples, 30+ dB SNR</li>
              <li>• Good: 30+ seconds, 3+ samples, 20+ dB SNR</li>
              <li>• Fair: 15+ seconds, 2+ samples, 15+ dB SNR</li>
              <li>• Speak clearly and avoid background noise</li>
              <li>• Include varied speech patterns and phrases</li>
            </ul>
          </div>
        </div>
      )}

      {/* Previous Sessions */}
      {sessions.length > 0 && !currentSession && (
        <div className="space-y-4">
          <h2 className="heading-md">Previous Enrollments</h2>
          <div className="space-y-3">
            {sessions.map((session) => (
              <div
                key={session.id}
                className="flex items-center justify-between p-4 card-secondary rounded-lg"
              >
                <div>
                  <p className="font-medium text-primary">{session.speakerName}</p>
                  <div className="flex items-center space-x-3 text-sm text-muted">
                    <span>{session.audioFiles.length} samples</span>
                    <span>{formatDuration(session.totalDuration)}</span>
                    <span className={`inline-flex items-center space-x-1 px-2 py-1 rounded-full text-xs ${getQualityColor(session.quality)}`}>
                      {getQualityIcon(session.quality)}
                      <span>{session.quality}</span>
                    </span>
                    <span className={`px-2 py-1 rounded-full text-xs ${
                      session.status === 'completed' ? 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200' :
                      session.status === 'processing' ? 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200' :
                      session.status === 'failed' ? 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200' :
                      'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200'
                    }`}>
                      {session.status}
                    </span>
                  </div>
                </div>
                <button
                  onClick={() => setCurrentSession(session)}
                  disabled={session.status === 'processing'}
                  className="px-3 py-1 text-blue-600 hover:text-blue-800 disabled:opacity-50 dark:text-blue-400 dark:hover:text-blue-300"
                >
                  {session.status === 'draft' ? 'Continue' : 'View'}
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Empty State */}
      {sessions.length === 0 && !currentSession && (
        <div className="text-center py-12">
          <Mic className="h-16 w-16 text-gray-300 dark:text-gray-500 mx-auto mb-4" />
          <h3 className="heading-sm mb-2">No Enrollments Yet</h3>
          <p className="text-muted mb-4">Start by creating a new speaker enrollment session.</p>
          <button
            onClick={() => {
              const name = prompt('Enter speaker name:')
              if (name?.trim()) {
                createNewSession(name.trim())
              }
            }}
            className="px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 dark:bg-blue-700 dark:hover:bg-blue-800"
          >
            Create First Enrollment
          </button>
        </div>
      )}
    </div>
  )
}
