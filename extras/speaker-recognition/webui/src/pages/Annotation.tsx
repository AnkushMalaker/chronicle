import { useState, useCallback, useRef, useEffect, useMemo } from 'react'
import { Play, Pause, Save, Download, CheckCircle, XCircle, AlertCircle, UserPlus, Filter, ChevronDown, X } from 'lucide-react'
import { useUser } from '../contexts/UserContext'
import { calculateFileHash, isAudioFile } from '../utils/fileHash'
import {
  loadAudioBuffer,
  createAudioContext,
  decodeAudioData,
  extractAudioSamples,
  formatDuration,
  createAudioBlob,
  extractAudioSegment,
  AudioSegment
} from '../utils/audioUtils'
import { newSpeakerId } from '../utils/common'
import { databaseService } from '../services/database'
import { apiService, type Annotation } from '../services/api'
import { speakerIdentificationService } from '../services/speakerIdentification'
import FileUploader from '../components/FileUploader'
import WaveformPlot from '../components/WaveformPlot'
import EmbeddingPlot from '../components/EmbeddingPlot'
import ProcessingModeSelector from '../components/ProcessingModeSelector'
import { useSpeakerIdentification } from '../hooks/useSpeakerIdentification'

interface AudioData {
  file: File
  buffer: AudioBuffer
  hash: string
  samples: Float32Array
}

interface AnnotationSegment extends AudioSegment {
  id: string
  speakerId?: string
  speakerLabel?: string
  deepgramSpeakerLabel?: string
  label: 'CORRECT' | 'INCORRECT' | 'UNCERTAIN'
  confidence?: number
  transcription?: string
  notes?: string
}

interface AvailableSpeaker {
  id?: string  // Only for enrolled speakers
  label: string
}

export default function Annotation() {
  const { user } = useUser()
  const [audioData, setAudioData] = useState<AudioData | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [segments, setSegments] = useState<AnnotationSegment[]>([])
  const [selectedSegmentId, setSelectedSegmentId] = useState<string | null>(null)

  // Add speaker processing hook for unified processing modes
  const speakerProcessing = useSpeakerIdentification({
    defaultMode: 'speaker-identification',
    onError: (error) => console.error('Processing error:', error),
  })
  const [availableSpeakers, setAvailableSpeakers] = useState<AvailableSpeaker[]>([
    { label: 'Speaker 1' },
    { label: 'Speaker 2' },
    { label: 'Unknown' }
  ])
  const [newSpeakerName, setNewSpeakerName] = useState('')
  const [playingSegmentId, setPlayingSegmentId] = useState<string | null>(null)
  const [hasUnsavedChanges, setHasUnsavedChanges] = useState(false)
  const [enrollingSpeaker, setEnrollingSpeaker] = useState<string | null>(null)
  const [deepgramResponse] = useState<any>(null)
  const [showJsonOutput, setShowJsonOutput] = useState(false)
  const [minSpeakers, setMinSpeakers] = useState(1)
  const [maxSpeakers, setMaxSpeakers] = useState(4)
  const [collar, setCollar] = useState(2.0)
  const [minDurationOff, setMinDurationOff] = useState(1.5)
  const [uploadedJson, setUploadedJson] = useState<any>(null)

  // Filter states
  const [selectedSpeakers, setSelectedSpeakers] = useState<Set<string>>(new Set())
  const [showSpeakerFilter, setShowSpeakerFilter] = useState(false)
  const [minDuration, setMinDuration] = useState(0)
  const [maxDuration, setMaxDuration] = useState(100)

  const audioContextRef = useRef<AudioContext | null>(null)
  const audioSourceRef = useRef<AudioBufferSourceNode | null>(null)

  // Update max duration when segments change
  useEffect(() => {
    if (segments.length > 0) {
      const max = Math.max(...segments.map(s => s.duration))
      setMaxDuration(Math.ceil(max * 10) / 10) // Round to 1 decimal place
    } else {
      setMaxDuration(100)
    }
  }, [segments])

  // Load enrolled speakers on mount
  useEffect(() => {
    const loadEnrolledSpeakers = async () => {
      if (!user) return

      try {
        const response = await apiService.get('/speakers', {
          params: { user_id: user.id }
        })

        const enrolledSpeakers = response.data.speakers || []

        // Combine enrolled speakers with default temporary labels
        const allSpeakers: AvailableSpeaker[] = [
          ...enrolledSpeakers.map((s: any) => ({ id: s.id, label: s.name })),
          { label: 'Speaker 1' },
          { label: 'Speaker 2' },
          { label: 'Unknown' }
        ]

        setAvailableSpeakers(allSpeakers)
      } catch (error) {
        console.error('Failed to load enrolled speakers:', error)
        // Keep default speakers on error
      }
    }

    loadEnrolledSpeakers()
  }, [user])

  // Calculate filtered segments based on speaker and duration filters
  const filteredSegments = useMemo(() => {
    return segments.filter(segment => {
      // Speaker filter
      if (selectedSpeakers.size > 0 && !selectedSpeakers.has(segment.speakerLabel || '')) {
        return false
      }

      // Duration filter
      if (segment.duration < minDuration || segment.duration > maxDuration) {
        return false
      }

      return true
    })
  }, [segments, selectedSpeakers, minDuration, maxDuration])

  // Get unique speakers and their counts
  const uniqueSpeakers = useMemo(() => {
    const speakerMap = new Map<string, number>()
    segments.forEach(segment => {
      const speaker = segment.speakerLabel || 'Unknown'
      speakerMap.set(speaker, (speakerMap.get(speaker) || 0) + 1)
    })
    return Array.from(speakerMap.entries()).map(([speaker, count]) => ({ speaker, count }))
  }, [segments])

  // Filter control functions
  const selectAllSpeakers = () => {
    setSelectedSpeakers(new Set(uniqueSpeakers.map(s => s.speaker)))
  }

  const clearAllSpeakers = () => {
    setSelectedSpeakers(new Set())
  }

  const clearAllFilters = () => {
    setSelectedSpeakers(new Set())
    setMinDuration(0)
    if (segments.length > 0) {
      const max = Math.max(...segments.map(s => s.duration))
      setMaxDuration(Math.ceil(max * 10) / 10)
    }
  }

  const hasActiveFilters = selectedSpeakers.size > 0 || minDuration > 0 ||
    (segments.length > 0 && maxDuration < Math.max(...segments.map(s => s.duration)))

  // Close speaker filter dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      const target = event.target as Element
      if (showSpeakerFilter && !target.closest('.speaker-filter-dropdown')) {
        setShowSpeakerFilter(false)
      }
    }

    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [showSpeakerFilter])


  // Handle processing with unified modes (diarization, deepgram, hybrid, plain)
  const handleProcessAudio = useCallback(async (mode: any) => {
    if (!audioData || !user) return

    try {
      // Validate requirements for diarize-identify-match mode
      if (mode === 'diarize-identify-match' && !uploadedJson) {
        alert('Please upload a transcript JSON file first to use Transcript + Diarize mode')
        return
      }

      // Process using the unified speaker identification service with additional options
      const options: any = {
        mode,
        userId: user.id,
        confidenceThreshold: speakerProcessing.confidenceThreshold,
        minDuration: 0.5,
        minSpeakers,
        maxSpeakers,
        collar,
        minDurationOff,
        identifyOnlyEnrolled: false,
        enhanceSpeakers: true,
        // Add transcript data when in diarize-identify-match mode
        ...(mode === 'diarize-identify-match' && uploadedJson && { transcriptData: uploadedJson })
      }

      const result = await speakerIdentificationService.processAudio(audioData.file, options)

      if (result && result.speakers) {
        // Convert processing result to annotation segments
        const newSegments: AnnotationSegment[] = result.speakers.map((speaker, index) => ({
          id: `${result.id}_segment_${index}_${Date.now()}`, // Create truly unique ID for each segment
          start: speaker.start,
          end: speaker.end,
          duration: speaker.end - speaker.start,
          speakerLabel: speaker.speaker_name || speaker.identified_speaker_name || `Speaker ${Math.floor(Math.random() * 1000)}`,
          deepgramSpeakerLabel: undefined,
          label: 'UNCERTAIN' as const,
          confidence: speaker.confidence,
          transcription: speaker.text,
          notes: undefined
        }))

        // Add unique speaker labels to available speakers
        const uniqueSpeakerLabels = new Set(
          newSegments
            .map(s => s.speakerLabel)
            .filter(label => label && !availableSpeakers.some(sp => sp.label === label))
        )

        if (uniqueSpeakerLabels.size > 0) {
          setAvailableSpeakers(prev => [
            ...prev,
            ...Array.from(uniqueSpeakerLabels).map(label => ({ label: label as string }))
          ])
        }

        setSegments(newSegments)
        setHasUnsavedChanges(true)
        alert(`Processed ${newSegments.length} segments using ${mode} mode`)
      }

    } catch (error) {
      console.error('Failed to process audio:', error)
      alert('Failed to process audio. Please try again.')
    }
  }, [audioData, user, availableSpeakers, speakerProcessing])

  const handleFileUpload = useCallback(async (files: File[]) => {
    if (!files.length || !user) return

    const file = files[0]
    if (!isAudioFile(file)) {
      alert('Please select a valid audio file (WAV, FLAC, MP3, M4A, OGG)')
      return
    }

    setIsLoading(true)
    try {
      // Calculate file hash
      const hash = await calculateFileHash(file)

      // Check if annotations exist for this file
      const hasExisting = await databaseService.hasAnnotations(hash, user.id)
      if (hasExisting) {
        const proceed = confirm(
          'Found existing annotations for this file. Load them? (Cancel to start fresh)'
        )
        if (proceed) {
          const existingAnnotations = await databaseService.loadAnnotations(hash, user.id)
          if (existingAnnotations) {
            // Convert database annotations to segments
            const loadedSegments: AnnotationSegment[] = existingAnnotations.map(ann => ({
              id: ann.id?.toString() || Math.random().toString(36),
              start: ann.start_time,
              end: ann.end_time,
              duration: ann.end_time - ann.start_time,
              speakerId: ann.speaker_id,
              speakerLabel: ann.speaker_label,
              deepgramSpeakerLabel: ann.deepgram_speaker_label,
              label: ann.label,
              confidence: ann.confidence,
              transcription: ann.transcription,
              notes: ann.notes
            }))
            setSegments(loadedSegments)

            // Also extract any unique speaker labels from loaded annotations
            const uniqueSpeakerLabels = new Set(
              loadedSegments
                .map(s => s.speakerLabel)
                .filter(label => label && !availableSpeakers.some(sp => sp.label === label))
            )

            // Add these to available speakers if they don't exist
            if (uniqueSpeakerLabels.size > 0) {
              setAvailableSpeakers(prev => [
                ...prev,
                ...Array.from(uniqueSpeakerLabels).map(label => ({ label: label as string }))
              ])
            }
          }
        }
      }

      // Load and decode audio
      const arrayBuffer = await loadAudioBuffer(file)
      const audioContext = createAudioContext()
      audioContextRef.current = audioContext

      const audioBuffer = await decodeAudioData(audioContext, arrayBuffer)
      const samples = extractAudioSamples(audioBuffer)

      setAudioData({
        file,
        buffer: audioBuffer,
        hash,
        samples
      })

      setHasUnsavedChanges(false)
    } catch (error) {
      console.error('Failed to process audio file:', error)
      alert('Failed to process audio file. Please try a different file.')
    } finally {
      setIsLoading(false)
    }
  }, [user])

  const handleDeepgramUpload = useCallback(async (files: File[]) => {
    const file = files[0]
    if (!file || !file.name.endsWith('.json')) {
      alert('Please select a JSON file')
      return
    }

    try {
      const text = await file.text()
      const deepgramData = JSON.parse(text)

      // Parse Deepgram format
      if (deepgramData.results?.channels?.[0]?.alternatives?.[0]?.words) {
        const words = deepgramData.results.channels[0].alternatives[0].words
        const newSegments: AnnotationSegment[] = []

        // Group words by speaker
        let currentSpeaker = null
        let currentSegmentStart = null
        let currentWords: any[] = []

        for (const word of words) {
          if (word.speaker !== currentSpeaker) {
            // Save previous segment
            if (currentSpeaker !== null && currentWords.length > 0) {
              const segmentEnd = currentWords[currentWords.length - 1].end
              newSegments.push({
                id: Math.random().toString(36),
                start: currentSegmentStart,
                end: segmentEnd,
                duration: segmentEnd - currentSegmentStart,
                speakerLabel: `Speaker ${currentSpeaker + 1}`,
                deepgramSpeakerLabel: `speaker_${currentSpeaker}`,
                label: 'UNCERTAIN' as const,
                confidence: Math.min(...currentWords.map(w => w.confidence || 0)),
                transcription: currentWords.map(w => w.word).join(' ')
              })
            }

            // Start new segment
            currentSpeaker = word.speaker
            currentSegmentStart = word.start
            currentWords = [word]
          } else {
            currentWords.push(word)
          }
        }

        // Add final segment
        if (currentWords.length > 0) {
          const segmentEnd = currentWords[currentWords.length - 1].end
          newSegments.push({
            id: Math.random().toString(36),
            start: currentSegmentStart,
            end: segmentEnd,
            duration: segmentEnd - currentSegmentStart,
            speakerLabel: `Speaker ${currentSpeaker + 1}`,
            deepgramSpeakerLabel: `speaker_${currentSpeaker}`,
            label: 'UNCERTAIN' as const,
            confidence: Math.min(...currentWords.map(w => w.confidence || 0)),
            transcription: currentWords.map(w => w.word).join(' ')
          })
        }

        // Store the JSON data for potential diarize-identify-match processing
        setUploadedJson(deepgramData)

        setSegments(newSegments)
        setHasUnsavedChanges(true)
        alert(`Loaded ${newSegments.length} segments from Deepgram JSON`)
      } else {
        alert('Invalid Deepgram JSON format')
      }
    } catch (error) {
      console.error('Failed to parse Deepgram JSON:', error)
      alert('Failed to parse Deepgram JSON file')
    }
  }, [])

  const addManualSegment = useCallback(() => {
    if (!audioData) return

    const newSegment: AnnotationSegment = {
      id: Math.random().toString(36),
      start: 0,
      end: Math.min(5, audioData.buffer.duration),
      duration: Math.min(5, audioData.buffer.duration),
      label: 'UNCERTAIN',
      speakerLabel: 'Unknown'
    }

    setSegments(prev => [...prev, newSegment])
    setSelectedSegmentId(newSegment.id)
    setHasUnsavedChanges(true)
  }, [audioData])

  const updateSegment = useCallback((id: string, updates: Partial<AnnotationSegment>) => {
    setSegments(prev => prev.map(seg =>
      seg.id === id ? { ...seg, ...updates } : seg
    ))
    setHasUnsavedChanges(true)
  }, [])

  const deleteSegment = useCallback((id: string) => {
    setSegments(prev => prev.filter(seg => seg.id !== id))
    if (selectedSegmentId === id) {
      setSelectedSegmentId(null)
    }
    setHasUnsavedChanges(true)
  }, [selectedSegmentId])

  const addSpeaker = useCallback(() => {
    if (!newSpeakerName.trim()) return

    // Add as temporary speaker (no ID)
    setAvailableSpeakers(prev => [...prev, { label: newSpeakerName.trim() }])
    setNewSpeakerName('')
  }, [newSpeakerName])

  const playSegment = useCallback(async (segment: AnnotationSegment) => {
    if (!audioData || !audioContextRef.current) return

    // Stop any currently playing audio
    if (audioSourceRef.current) {
      audioSourceRef.current.stop()
      audioSourceRef.current = null
    }

    try {
      const source = audioContextRef.current.createBufferSource()
      source.buffer = audioData.buffer
      source.connect(audioContextRef.current.destination)

      source.start(0, segment.start, segment.duration)
      audioSourceRef.current = source
      setPlayingSegmentId(segment.id)

      source.onended = () => {
        setPlayingSegmentId(null)
        audioSourceRef.current = null
      }
    } catch (error) {
      console.error('Failed to play segment:', error)
      setPlayingSegmentId(null)
    }
  }, [audioData])

  const stopPlayback = useCallback(() => {
    if (audioSourceRef.current) {
      audioSourceRef.current.stop()
      audioSourceRef.current = null
      setPlayingSegmentId(null)
    }
  }, [])

  const saveAnnotations = useCallback(async () => {
    if (!audioData || !user) return

    try {
      const annotations: Annotation[] = segments.map(seg => ({
        audio_file_path: audioData.file.name,
        audio_file_hash: audioData.hash,
        audio_file_name: audioData.file.name,
        start_time: seg.start,
        end_time: seg.end,
        speaker_id: seg.speakerId,
        speaker_label: seg.speakerLabel,
        deepgram_speaker_label: seg.deepgramSpeakerLabel,
        label: seg.label,
        confidence: seg.confidence,
        transcription: seg.transcription,
        user_id: user.id,
        notes: seg.notes
      }))

      await databaseService.saveAnnotations(
        audioData.hash,
        audioData.file.name,
        annotations,
        user.id
      )

      setHasUnsavedChanges(false)
      alert('Annotations saved successfully!')
    } catch (error) {
      console.error('Failed to save annotations:', error)
      alert('Failed to save annotations')
    }
  }, [audioData, user, segments])

  const exportAnnotations = useCallback(() => {
    if (!segments.length) return

    const exportData = {
      filename: audioData?.file.name,
      hash: audioData?.hash,
      segments: segments.map(seg => ({
        start: seg.start,
        end: seg.end,
        duration: seg.duration,
        speaker: seg.speakerLabel,
        speaker_id: seg.speakerId,
        label: seg.label,
        confidence: seg.confidence,
        transcription: seg.transcription,
        notes: seg.notes
      }))
    }

    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `annotations_${audioData?.file.name.split('.')[0]}.json`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }, [segments, audioData])

  const exportSpeakerSegments = useCallback(async (speakerLabel: string) => {
    if (!audioData || !segments.length) return

    const speakerSegments = segments.filter(s => s.speakerLabel === speakerLabel)
    if (speakerSegments.length === 0) {
      alert(`No segments found for ${speakerLabel}`)
      return
    }

    // Create export data with segment information
    const exportData = {
      speaker_label: speakerLabel,
      audio_file: audioData.file.name,
      total_segments: speakerSegments.length,
      total_duration: speakerSegments.reduce((sum, seg) => sum + seg.duration, 0),
      segments: speakerSegments.map(seg => ({
        start: seg.start,
        end: seg.end,
        duration: seg.duration,
        transcription: seg.transcription,
        confidence: seg.confidence
      }))
    }

    // For now, export as JSON metadata
    // TODO: In future, could create actual audio files with segments
    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `speaker_segments_${speakerLabel.replace(/\s+/g, '_')}_${Date.now()}.json`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)

    alert(`Exported ${speakerSegments.length} segments for ${speakerLabel}`)
  }, [segments, audioData])

  const enrollSpeaker = useCallback(async (speakerLabel: string, speakerId?: string) => {
    if (!audioData || !user) return

    const speakerSegments = segments.filter(s => s.speakerLabel === speakerLabel)
    if (speakerSegments.length === 0) {
      alert(`No segments found for ${speakerLabel}`)
      return
    }

    // If speaker is already enrolled, ask user what to do
    if (speakerId) {
      const action = window.confirm(
        `"${speakerLabel}" is already enrolled.\n\n` +
        `Click OK to add these segments to the existing enrollment.\n` +
        `Click Cancel to create a fresh enrollment (will replace the existing one).`
      )

      if (!action) {
        // User chose to create fresh enrollment
        const confirmReplace = window.confirm(
          `Are you sure you want to replace the existing enrollment for "${speakerLabel}"?\n\n` +
          `This will delete the current speaker embeddings and create new ones.`
        )
        if (!confirmReplace) return
        speakerId = undefined // Will create new enrollment
      }
    }

    setEnrollingSpeaker(speakerLabel)

    try {
      // Extract audio segments and create blobs
      console.log(`Processing ${speakerSegments.length} segments for enrollment...`)
      const audioBlobs: Blob[] = []

      for (let i = 0; i < speakerSegments.length; i++) {
        const segment = speakerSegments[i]
        console.log(`Extracting segment ${i + 1}: ${segment.start}s - ${segment.end}s`)

        try {
          const audioSamples = extractAudioSamples(audioData.buffer)
          const samples = extractAudioSegment(audioSamples, segment.start, segment.end, audioData.buffer.sampleRate)

          if (samples.length === 0) {
            console.warn(`Segment ${i + 1} resulted in empty audio, skipping`)
            continue
          }

          const blob = createAudioBlob(samples, audioData.buffer.sampleRate)
          audioBlobs.push(blob)
          console.log(`Successfully created ${blob.size} byte WAV blob for segment ${i + 1}`)
        } catch (error) {
          console.error(`Failed to extract segment ${i + 1}:`, error)
          alert(`❌ Failed to extract audio segment ${i + 1}: ${error instanceof Error ? error.message : 'Unknown error'}`)
          return
        }
      }

      if (audioBlobs.length === 0) {
        alert('❌ No valid audio segments could be extracted. Please check that the selected segments contain audio.')
        return
      }

      console.log(`Successfully extracted ${audioBlobs.length} audio segments`)

      // Create FormData for enrollment
      const formData = new FormData()
      const targetSpeakerId = speakerId || newSpeakerId()

      formData.append('speaker_id', targetSpeakerId)
      formData.append('speaker_name', speakerLabel)
      formData.append('user_id', String(user.id))

      audioBlobs.forEach((blob, index) => {
        formData.append('files', blob, `segment_${String(index + 1).padStart(3, '0')}.wav`)
      })

      console.log(`Enrolling speaker with ID: ${targetSpeakerId}`)

      // Submit enrollment - use append endpoint if speaker exists, batch if new
      const endpoint = speakerId ? '/enroll/append' : '/enroll/batch'
      console.log(`Using endpoint: ${endpoint}`)

      await apiService.post(endpoint, formData, {
        headers: {
          'Content-Type': 'multipart/form-data',
        },
        timeout: 120000, // 2 minutes for enrollment operations
      })

      // Refresh speakers list to get the updated enrolled speakers
      try {
        const response = await apiService.get('/speakers', {
          params: { user_id: user.id }
        })

        const enrolledSpeakers = response.data.speakers || []

        // Combine enrolled speakers with default temporary labels
        const allSpeakers: AvailableSpeaker[] = [
          ...enrolledSpeakers.map((s: any) => ({ id: s.id, label: s.name })),
          { label: 'Speaker 1' },
          { label: 'Speaker 2' },
          { label: 'Unknown' }
        ]

        setAvailableSpeakers(allSpeakers)
      } catch (error) {
        console.error('Failed to refresh speakers list:', error)
      }

      alert(
        `✅ Speaker "${speakerLabel}" enrolled successfully!\n\n` +
        `Processed ${speakerSegments.length} segments (${formatDuration(speakerSegments.reduce((sum, seg) => sum + seg.duration, 0) * 1000)} total)`
      )

    } catch (error) {
      console.error('Failed to enroll speaker:', error)

      // Handle specific error cases
      let errorMessage = `❌ Failed to enroll speaker "${speakerLabel}".`

      // Check for axios error with response
      if (error && typeof error === 'object' && 'response' in error) {
        const axiosError = error as any
        if (axiosError.response?.status === 400 && axiosError.response?.data?.detail) {
          // Use the detailed error message from the backend
          errorMessage = `❌ ${axiosError.response.data.detail}`
        } else if (axiosError.response?.data?.detail) {
          errorMessage += ` ${axiosError.response.data.detail}`
        } else if (axiosError.message) {
          errorMessage += ` ${axiosError.message}`
        }
      } else if (error instanceof Error) {
        if (error.message.includes('already exists')) {
          errorMessage = `❌ Speaker name "${speakerLabel}" already exists for this user. Please choose a different name.`
        } else {
          errorMessage += ` ${error.message}`
        }
      } else {
        errorMessage += ' Please try again.'
      }

      alert(errorMessage)
    } finally {
      setEnrollingSpeaker(null)
    }
  }, [audioData, user, segments])

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
        <h1 className="text-2xl font-bold text-primary">📝 Annotation Tool</h1>
        {hasUnsavedChanges && (
          <div className="flex items-center space-x-2 text-amber-600 dark:text-amber-400">
            <AlertCircle className="h-5 w-5" />
            <span className="text-sm font-medium">Unsaved changes</span>
          </div>
        )}
      </div>

      <p className="text-secondary">
        Upload audio files and create speaker annotations with timeline visualization.
      </p>

      {/* File Upload */}
      <div className="grid md:grid-cols-2 gap-6">
        <div className="border-2 border-dashed border-gray-300 dark:border-gray-600 rounded-lg p-6">
          <h3 className="text-lg font-medium mb-4 text-primary">📁 Upload Audio File</h3>
          <FileUploader
            onUpload={handleFileUpload}
            accept=".wav,.flac,.mp3,.m4a,.ogg"
            multiple={false}
            disabled={isLoading}
          />
        </div>

        {/* Import JSON */}
        <div className="card-2 p-6">
          <h3 className="text-lg font-medium mb-4 text-primary">📄 Import Deepgram JSON</h3>
          <FileUploader
            onUpload={handleDeepgramUpload}
            accept=".json"
            multiple={false}
            disabled={!audioData}
            title="Upload Transcript JSON"
          />
          {!audioData && (
            <p className="text-sm text-muted mt-2">Upload audio file first</p>
          )}
        </div>
      </div>

      {/* Processing Mode Selector - Full Width Section */}
      <div className="border-2 border-dashed border-green-300 dark:border-green-600 rounded-lg p-6">
        <h3 className="text-xl font-medium mb-4 text-primary">🎯 Speaker Processing Modes</h3>

        {audioData ? (
          <ProcessingModeSelector
            selectedMode={speakerProcessing.currentMode}
            onModeChange={speakerProcessing.setProcessingMode}
            onProcessAudio={handleProcessAudio}
            audioData={{
              file: audioData.file,
              filename: audioData.file.name,
              buffer: {
                samples: audioData.samples,
                sampleRate: audioData.buffer.sampleRate,
                channels: audioData.buffer.numberOfChannels,
                duration: audioData.buffer.duration
              },
              quality: undefined
            }}
            isProcessing={speakerProcessing.isProcessing}
            confidenceThreshold={speakerProcessing.confidenceThreshold}
            onConfidenceThresholdChange={speakerProcessing.setConfidenceThreshold}
            minSpeakers={minSpeakers}
            onMinSpeakersChange={setMinSpeakers}
            maxSpeakers={maxSpeakers}
            onMaxSpeakersChange={setMaxSpeakers}
            collar={collar}
            onCollarChange={setCollar}
            minDurationOff={minDurationOff}
            onMinDurationOffChange={setMinDurationOff}
            uploadedJson={uploadedJson}
            showSettings={true}
            compact={true}
          />
        ) : (
          <div className="text-center py-12 text-muted">
            <p className="text-lg">Upload audio file first to enable processing modes</p>
          </div>
        )}
      </div>

      {isLoading && (
        <div className="text-center py-8">
          <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600 dark:border-blue-400"></div>
          <p className="mt-2 text-secondary">Processing audio file...</p>
        </div>
      )}

      {audioData && (
        <>
          {/* Audio Information */}
          <div className="card-secondary p-4">
            <h3 className="mb-2 heading-sm">🎵 {audioData.file.name}</h3>
            <div className="grid grid-cols-3 gap-4 text-sm text-secondary">
              <div>Duration: {formatDuration(audioData.buffer.duration * 1000)}</div>
              <div>Sample Rate: {(audioData.buffer.sampleRate / 1000).toFixed(1)} kHz</div>
              <div>Segments: {segments.length}</div>
            </div>
          </div>

          {/* Waveform with segments */}
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="heading-sm">📊 Timeline</h3>
              <button
                onClick={addManualSegment}
                className="px-4 py-2 btn-2"
              >
                Add Segment
              </button>
            </div>

            {/* WaveformPlot renders the waveform only; segment overlay/selection is
                handled by the segment list below (these props were never consumed). */}
            <WaveformPlot
              samples={audioData.samples}
              sampleRate={audioData.buffer.sampleRate}
            />
          </div>

          {/* Segments List */}
          <div className="space-y-4">
            <h3 className="heading-sm">🗣️ Speaker Segments</h3>

            {/* Filter Controls */}
            {segments.length > 0 && (
              <div className="flex flex-wrap items-center gap-4 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg border dark:border-gray-700">
                {/* Speaker Filter */}
                <div className="relative speaker-filter-dropdown">
                  <button
                    onClick={() => setShowSpeakerFilter(!showSpeakerFilter)}
                    className="flex items-center space-x-2 px-3 py-2 bg-white dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded-md hover:bg-gray-50 dark:hover:bg-gray-600 transition-colors"
                  >
                    <Filter className="h-4 w-4" />
                    <span className="text-sm">Filter Speakers</span>
                    {selectedSpeakers.size > 0 && (
                      <span className="bg-blue-100 text-blue-800 text-xs px-2 py-1 rounded-full dark:bg-blue-900 dark:text-blue-200">
                        {selectedSpeakers.size}
                      </span>
                    )}
                    <ChevronDown className="h-4 w-4" />
                  </button>

                  {showSpeakerFilter && (
                    <div
                      className="absolute z-10 mt-1 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded-lg p-3 min-w-48 shadow-lg"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <div className="space-y-2 max-h-40 overflow-y-auto">
                        {uniqueSpeakers.map(({ speaker, count }) => (
                          <label
                            key={speaker}
                            className="flex items-center space-x-2 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700 p-1 rounded"
                            onClick={(e) => e.stopPropagation()}
                          >
                            <input
                              type="checkbox"
                              checked={selectedSpeakers.has(speaker)}
                              onChange={(e) => {
                                e.stopPropagation()
                                const newSet = new Set(selectedSpeakers)
                                if (e.target.checked) {
                                  newSet.add(speaker)
                                } else {
                                  newSet.delete(speaker)
                                }
                                setSelectedSpeakers(newSet)
                              }}
                              className="rounded"
                            />
                            <span className="text-sm">{speaker}</span>
                            <span className="text-xs text-muted">({count})</span>
                          </label>
                        ))}
                      </div>
                      <div className="flex gap-2 mt-3 pt-2 border-t border-gray-200 dark:border-gray-600">
                        <button
                          onClick={(e) => {
                            e.stopPropagation()
                            selectAllSpeakers()
                          }}
                          className="flex-1 px-2 py-1 text-xs bg-blue-100 text-blue-800 rounded hover:bg-blue-200 dark:bg-blue-900 dark:text-blue-200 dark:hover:bg-blue-800"
                        >
                          Select All
                        </button>
                        <button
                          onClick={(e) => {
                            e.stopPropagation()
                            clearAllSpeakers()
                          }}
                          className="flex-1 px-2 py-1 text-xs bg-gray-100 text-gray-800 rounded hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600"
                        >
                          Clear All
                        </button>
                      </div>
                    </div>
                  )}
                </div>

                {/* Duration Range Filter */}
                <div className="flex items-center gap-2">
                  <label className="text-sm font-medium">Duration:</label>
                  <input
                    type="number"
                    value={minDuration.toFixed(1)}
                    onChange={(e) => setMinDuration(Math.max(0, parseFloat(e.target.value) || 0))}
                    className="w-16 px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 h-8"
                    placeholder="Min"
                    step="0.1"
                    min="0"
                  />
                  <div className="relative w-32 h-8 flex items-center">
                    {/* Background track */}
                    <div className="absolute w-full h-2 bg-gray-200 dark:bg-gray-600 rounded-lg"></div>

                    {/* Active range */}
                    <div
                      className="absolute h-2 bg-blue-500 rounded-lg"
                      style={{
                        left: `${(minDuration / (segments.length > 0 ? Math.max(...segments.map(s => s.duration)) : 100)) * 100}%`,
                        width: `${((maxDuration - minDuration) / (segments.length > 0 ? Math.max(...segments.map(s => s.duration)) : 100)) * 100}%`
                      }}
                    ></div>

                    {/* Min handle */}
                    <input
                      type="range"
                      min="0"
                      max={segments.length > 0 ? Math.max(...segments.map(s => s.duration)) : 100}
                      step="0.1"
                      value={minDuration}
                      onChange={(e) => {
                        const value = parseFloat(e.target.value)
                        if (value <= maxDuration) {
                          setMinDuration(value)
                        }
                      }}
                      className="absolute w-full h-2 bg-transparent rounded-lg appearance-none cursor-pointer range-slider-min"
                      style={{
                        zIndex: minDuration > maxDuration - 1 ? 5 : 3
                      }}
                    />

                    {/* Max handle */}
                    <input
                      type="range"
                      min="0"
                      max={segments.length > 0 ? Math.max(...segments.map(s => s.duration)) : 100}
                      step="0.1"
                      value={maxDuration}
                      onChange={(e) => {
                        const value = parseFloat(e.target.value)
                        if (value >= minDuration) {
                          setMaxDuration(value)
                        }
                      }}
                      className="absolute w-full h-2 bg-transparent rounded-lg appearance-none cursor-pointer range-slider-max"
                      style={{
                        zIndex: maxDuration < minDuration + 1 ? 5 : 4
                      }}
                    />
                  </div>
                  <input
                    type="number"
                    value={maxDuration.toFixed(1)}
                    onChange={(e) => {
                      const value = parseFloat(e.target.value) || 0
                      const maxPossible = segments.length > 0 ? Math.max(...segments.map(s => s.duration)) : 100
                      setMaxDuration(Math.min(maxPossible, Math.max(minDuration, value)))
                    }}
                    className="w-16 px-2 py-1 text-sm border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-700 h-8"
                    placeholder="Max"
                    step="0.1"
                    min={minDuration}
                  />
                  <span className="text-sm text-muted">
                    ({filteredSegments.length} segments)
                  </span>
                </div>

                {/* Clear Filters */}
                {hasActiveFilters && (
                  <button
                    onClick={clearAllFilters}
                    className="flex items-center space-x-1 px-2 py-1 text-sm text-blue-600 hover:text-blue-800 hover:bg-blue-50 dark:text-blue-400 dark:hover:text-blue-300 dark:hover:bg-blue-900 rounded transition-colors"
                  >
                    <X className="h-3 w-3" />
                    <span>Clear Filters</span>
                  </button>
                )}
              </div>
            )}

            <div className="space-y-3 max-h-96 overflow-y-auto">
              {filteredSegments.map((segment) => (
                <SegmentEditor
                  key={segment.id}
                  segment={segment}
                  isSelected={selectedSegmentId === segment.id}
                  isPlaying={playingSegmentId === segment.id}
                  availableSpeakers={availableSpeakers}
                  onSelect={() => setSelectedSegmentId(segment.id)}
                  onUpdate={(updates) => updateSegment(segment.id, updates)}
                  onDelete={() => deleteSegment(segment.id)}
                  onPlay={() => playSegment(segment)}
                  onStop={stopPlayback}
                />
              ))}
            </div>
          </div>

          {/* Speaker Management */}
          <div className="card-secondary p-4">
            <h3 className="text-lg font-medium mb-4 dark:text-gray-100">👥 Manage Speakers</h3>
            <div className="flex space-x-2">
              <input
                type="text"
                value={newSpeakerName}
                onChange={(e) => setNewSpeakerName(e.target.value)}
                placeholder="New speaker name"
                className="flex-1 px-3 py-2 border border-gray-300 rounded-md dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200"
                onKeyPress={(e) => e.key === 'Enter' && addSpeaker()}
              />
              <button
                onClick={addSpeaker}
                disabled={!newSpeakerName.trim()}
                className="px-4 py-2 bg-green-600 text-white rounded-md hover:bg-green-700 disabled:opacity-50 dark:bg-green-700 dark:hover:bg-green-800"
              >
                Add Speaker
              </button>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              {availableSpeakers.map((speaker, index) => (
                <span
                  key={index}
                  className={`px-3 py-1 rounded-full text-sm ${
                    speaker.id
                      ? 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200'
                      : 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200'
                  }`}
                  title={speaker.id ? 'Enrolled speaker' : 'Temporary label'}
                >
                  {speaker.label}
                </span>
              ))}
            </div>
          </div>

          {/* Speaker Segments Summary */}
          {segments.length > 0 && (
            <div className="card-secondary p-4">
              <h3 className="text-lg font-medium mb-4 dark:text-gray-100">📊 Speaker Segments Summary</h3>
              <div className="space-y-2">
                {Array.from(new Set(segments.map(s => s.speakerLabel).filter(Boolean))).map(speakerLabel => {
                  const speakerSegments = segments.filter(s => s.speakerLabel === speakerLabel)
                  const totalDuration = speakerSegments.reduce((sum, seg) => sum + seg.duration, 0)
                  const speaker = availableSpeakers.find(s => s.label === speakerLabel)

                  return (
                    <div key={speakerLabel} className="flex items-center justify-between p-2 card">
                      <div className="flex items-center space-x-2">
                        <span className={`px-2 py-1 rounded-full text-xs ${
                          speaker?.id
                            ? 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200'
                            : 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200'
                        }`}>
                          {speakerLabel}
                        </span>
                        <span className="text-sm text-secondary">
                          {speakerSegments.length} segments • {formatDuration(totalDuration * 1000)}
                        </span>
                      </div>
                      <div className="flex space-x-2">
                        <button
                          onClick={() => enrollSpeaker(speakerLabel!, speaker?.id)}
                          disabled={enrollingSpeaker === speakerLabel}
                          className="flex items-center space-x-1 px-3 py-1 text-sm bg-green-600 text-white rounded hover:bg-green-700 disabled:opacity-50 dark:bg-green-700 dark:hover:bg-green-800"
                        >
                          <UserPlus className="h-3 w-3" />
                          <span>
                            {enrollingSpeaker === speakerLabel
                              ? 'Enrolling...'
                              : speaker?.id
                                ? 'Update Enrollment'
                                : 'Enroll Speaker'}
                          </span>
                        </button>
                        <button
                          onClick={() => exportSpeakerSegments(speakerLabel!)}
                          className="flex items-center space-x-1 px-2 py-1 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 dark:bg-blue-700 dark:hover:bg-blue-800"
                        >
                          <Download className="h-3 w-3" />
                          <span>Export</span>
                        </button>
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Embedding Analysis Section */}
          <div className="space-y-4">
            <EmbeddingPlot
              dataSource={{
                type: 'combined',
                segments: segments,
                audioFile: audioData?.file || new File([], ''),
                userId: user?.id
              }}
              compact={true}
              title="📊 Combined Analysis: Segments vs Enrolled Speakers"
              autoAnalyze={false}
              onAnalysisComplete={(analysis) => {
                console.log('Combined embedding analysis completed:', analysis)

                // Show smart suggestion if available
                if (analysis.smart_suggestion) {
                  console.log('Smart threshold suggestion:', analysis.smart_suggestion)
                }
              }}
            />
          </div>

          {/* Save and Export Actions */}
          <div className="flex justify-end space-x-3 pt-6 border-t dark:border-gray-700">
            {deepgramResponse && (
              <button
                onClick={() => setShowJsonOutput(!showJsonOutput)}
                className={`flex items-center space-x-2 px-4 py-2 rounded-md ${
                  showJsonOutput
                    ? 'bg-blue-600 text-white hover:bg-blue-700 dark:bg-blue-700 dark:hover:bg-blue-800'
                    : 'bg-gray-100 text-gray-700 hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600'
                }`}
              >
                <span>📄</span>
                <span>{showJsonOutput ? 'Hide' : 'Show'} JSON</span>
              </button>
            )}
            <button
              onClick={saveAnnotations}
              disabled={!hasUnsavedChanges || segments.length === 0}
              className="flex items-center space-x-2 px-6 py-3 bg-green-600 text-white rounded-md hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed dark:bg-green-700 dark:hover:bg-green-800"
            >
              <Save className="h-4 w-4" />
              <span>Save Annotations</span>
            </button>
            <button
              onClick={exportAnnotations}
              disabled={segments.length === 0}
              className="flex items-center space-x-2 px-6 py-3 bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed dark:bg-blue-700 dark:hover:bg-blue-800"
            >
              <Download className="h-4 w-4" />
              <span>Export JSON</span>
            </button>
          </div>

          {/* JSON Output Section */}
          {showJsonOutput && deepgramResponse && (
            <div className="mt-6">
              <div className="flex items-center justify-between mb-4">
                <h4 className="font-medium text-primary">🔍 Debug Output</h4>
                <button
                  onClick={() => setShowJsonOutput(!showJsonOutput)}
                  className="flex items-center space-x-2 px-3 py-1 text-sm bg-gray-100 text-gray-700 rounded-md hover:bg-gray-200 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600"
                >
                  <span>{showJsonOutput ? 'Hide' : 'Show'} JSON</span>
                </button>
              </div>

              <div className="space-y-4">
                {/* Deepgram Response */}
                <div className="card-secondary p-4">
                  <h5 className="font-medium text-primary mb-2">Deepgram API Response</h5>
                  <pre className="text-xs card p-3 overflow-auto max-h-64 text-primary text-left">
                    {JSON.stringify(deepgramResponse, null, 2)}
                  </pre>
                </div>

                {/* Annotation Segments */}
                <div className="card-secondary p-4">
                  <h5 className="font-medium text-primary mb-2">Annotation Segments</h5>
                  <pre className="text-xs card p-3 overflow-auto max-h-64 text-primary text-left">
                    {JSON.stringify(segments, null, 2)}
                  </pre>
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}

interface SegmentEditorProps {
  segment: AnnotationSegment
  isSelected: boolean
  isPlaying: boolean
  availableSpeakers: AvailableSpeaker[]
  onSelect: () => void
  onUpdate: (updates: Partial<AnnotationSegment>) => void
  onDelete: () => void
  onPlay: () => void
  onStop: () => void
}

function SegmentEditor({
  segment,
  isSelected,
  isPlaying,
  availableSpeakers,
  onSelect,
  onUpdate,
  onDelete,
  onPlay,
  onStop
}: SegmentEditorProps) {
  const getConfidenceColor = (confidence?: number) => {
    if (!confidence) return 'text-gray-400 dark:text-gray-500'
    if (confidence >= 0.7) return 'text-green-600 dark:text-green-400'
    if (confidence >= 0.4) return 'text-yellow-600 dark:text-yellow-400'
    return 'text-red-600 dark:text-red-400'
  }

  const getConfidenceLabel = (confidence?: number) => {
    if (!confidence) return 'N/A'
    if (confidence >= 0.7) return 'High'
    if (confidence >= 0.4) return 'Med'
    return 'Low'
  }

  const getLabelIcon = (label: string) => {
    switch (label) {
      case 'CORRECT': return <CheckCircle className="h-4 w-4 text-green-500 dark:text-green-400" />
      case 'INCORRECT': return <XCircle className="h-4 w-4 text-red-500 dark:text-red-400" />
      default: return <AlertCircle className="h-4 w-4 text-yellow-500 dark:text-yellow-400" />
    }
  }

  const getLabelColor = (label: string) => {
    switch (label) {
      case 'CORRECT': return 'border-green-200 dark:border-green-700 bg-green-50 dark:bg-green-900'
      case 'INCORRECT': return 'border-red-200 dark:border-red-700 bg-red-50 dark:bg-red-900'
      default: return 'border-yellow-200 dark:border-yellow-700 bg-yellow-50 dark:bg-yellow-900'
    }
  }

  const getTextColor = (isSelected: boolean, label: string) => {
    if (isSelected) return 'text-blue-800 dark:text-blue-200'
    switch (label) {
      case 'CORRECT': return 'text-green-800 dark:text-green-200'
      case 'INCORRECT': return 'text-red-800 dark:text-red-200'
      default: return 'text-yellow-800 dark:text-yellow-200' // UNCERTAIN
    }
  }

  return (
    <div
      className={`border rounded-lg p-4 cursor-pointer transition-colors ${
        isSelected
          ? 'border-blue-500 bg-blue-50 dark:border-blue-400 dark:bg-blue-900'
          : getLabelColor(segment.label)
      }`}
      onClick={onSelect}
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center space-x-2">
          {getLabelIcon(segment.label)}
          <span className={`font-medium ${getTextColor(isSelected, segment.label)}`}>
            {segment.start.toFixed(2)}s - {segment.end.toFixed(2)}s
          </span>
          <span className={`text-sm ${getTextColor(isSelected, segment.label)}`}>
            ({formatDuration(segment.duration * 1000)})
          </span>
          {segment.confidence !== undefined && (
            <span className={`text-xs px-2 py-1 rounded-full bg-gray-100 dark:bg-gray-700 font-medium ${getConfidenceColor(segment.confidence)}`}>
              {getConfidenceLabel(segment.confidence)} {(segment.confidence * 100).toFixed(0)}%
            </span>
          )}
        </div>

        <div className="flex space-x-2">
          <button
            onClick={(e) => {
              e.stopPropagation()
              isPlaying ? onStop() : onPlay()
            }}
            className={`p-1 rounded ${
              isPlaying
                ? 'bg-red-100 dark:bg-red-900 text-red-700 dark:text-red-300 hover:bg-red-200 dark:hover:bg-red-800'
                : 'bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300 hover:bg-blue-200 dark:hover:bg-blue-800'
            }`}
          >
            {isPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation()
              onDelete()
            }}
            className="p-1 bg-red-100 dark:bg-red-900 text-red-700 dark:text-red-300 rounded hover:bg-red-200 dark:hover:bg-red-800"
          >
            <XCircle className="h-4 w-4" />
          </button>
        </div>
      </div>

      {isSelected && (
        <div className="space-y-3 border-t border-gray-200 dark:border-gray-600 pt-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Speaker</label>
              <select
                value={segment.speakerLabel || ''}
                onChange={(e) => {
                  const selectedLabel = e.target.value
                  const selectedSpeaker = availableSpeakers.find(s => s.label === selectedLabel)
                  onUpdate({
                    speakerLabel: selectedLabel,
                    speakerId: selectedSpeaker?.id // Will be undefined for temporary labels
                  })
                }}
                className="w-full px-2 py-1 border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 rounded text-sm"
              >
                <option value="">Select speaker...</option>
                {availableSpeakers.map((speaker, index) => (
                  <option key={speaker.id ? `enrolled-${speaker.id}` : `temp-${index}-${speaker.label}`} value={speaker.label}>
                    {speaker.label} {speaker.id ? '✓' : ''}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="input-label mb-1">Label</label>
              <select
                value={segment.label}
                onChange={(e) => onUpdate({ label: e.target.value as any })}
                className="w-full px-2 py-1 border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 rounded text-sm"
              >
                <option value="CORRECT">Correct</option>
                <option value="INCORRECT">Incorrect</option>
                <option value="UNCERTAIN">Uncertain</option>
              </select>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1 dark:text-gray-300">Transcription</label>
            <textarea
              value={segment.transcription || ''}
              onChange={(e) => onUpdate({ transcription: e.target.value })}
              placeholder="Enter transcription..."
              className="w-full px-2 py-1 border border-gray-300 rounded text-sm dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200"
              rows={2}
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1 text-secondary">Notes</label>
            <input
              type="text"
              value={segment.notes || ''}
              onChange={(e) => onUpdate({ notes: e.target.value })}
              placeholder="Optional notes..."
              className="w-full px-2 py-1 border border-gray-300 rounded text-sm dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200"
            />
          </div>

          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="input-label mb-1">Start (s)</label>
              <input
                type="number"
                step="0.1"
                value={segment.start}
                onChange={(e) => {
                  const start = parseFloat(e.target.value)
                  onUpdate({
                    start,
                    duration: segment.end - start
                  })
                }}
                className="w-full px-2 py-1 border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 rounded text-sm"
              />
            </div>

            <div>
              <label className="input-label mb-1">End (s)</label>
              <input
                type="number"
                step="0.1"
                value={segment.end}
                onChange={(e) => {
                  const end = parseFloat(e.target.value)
                  onUpdate({
                    end,
                    duration: end - segment.start
                  })
                }}
                className="w-full px-2 py-1 border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 rounded text-sm"
              />
            </div>

            <div>
              <label className="input-label mb-1">
                Confidence {segment.confidence && `(${getConfidenceLabel(segment.confidence)})`}
              </label>
              <input
                type="number"
                step="0.01"
                min="0"
                max="1"
                value={segment.confidence || ''}
                onChange={(e) => onUpdate({ confidence: parseFloat(e.target.value) || undefined })}
                className={`w-full px-2 py-1 border rounded text-sm ${
                  segment.confidence
                    ? segment.confidence >= 0.7
                      ? 'border-green-300 bg-green-50 dark:border-green-700 dark:bg-green-900'
                      : segment.confidence >= 0.4
                        ? 'border-yellow-300 bg-yellow-50 dark:border-yellow-700 dark:bg-yellow-900'
                        : 'border-red-300 bg-red-50 dark:border-red-700 dark:bg-red-900'
                    : 'border-gray-300 dark:border-gray-600'
                }`}
              />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
