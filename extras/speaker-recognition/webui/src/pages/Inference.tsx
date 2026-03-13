/**
 * Inference Page - Refactored to use shared components
 * Now supports all processing modes with dramatically reduced code complexity
 */

import React, { useState } from 'react'
import { Upload, Users } from 'lucide-react'
import { useUser } from '../contexts/UserContext'
import { useAudioRecording } from '../hooks/useAudioRecording'
import { useSpeakerIdentification } from '../hooks/useSpeakerIdentification'
import { audioProcessingService } from '../services/audioProcessing'
import FileUploader from '../components/FileUploader'
import AudioRecordingControls from '../components/AudioRecordingControls'
import ProcessingModeSelector from '../components/ProcessingModeSelector'
import SpeakerResultsDisplay from '../components/SpeakerResultsDisplay'
import WaveformPlot from '../components/WaveformPlot'

export default function Inference() {
  const { user } = useUser()

  // State for uploaded audio (separate from recorded audio)
  const [uploadedAudio, setUploadedAudio] = useState<any>(null)

  // State for diarization parameters
  const [minSpeakers, setMinSpeakers] = useState(1)
  const [maxSpeakers, setMaxSpeakers] = useState(4)
  const [collar, setCollar] = useState(2.0)
  const [minDurationOff, setMinDurationOff] = useState(1.5)

  // Use our new shared hooks
  const recording = useAudioRecording({
    onError: (error) => console.error('Recording error:', error),
    onRecordingStart: () => setUploadedAudio(null), // Clear uploaded audio when recording starts
  })

  const speakerProcessing = useSpeakerIdentification({
    defaultMode: 'speaker-identification',
    userId: user?.id,
    onError: (error) => console.error('Processing error:', error),
  })

  // Handle file upload
  const handleFileUpload = async (files: File[]) => {
    if (!files.length || !user) return

    const file = files[0]
    if (!audioProcessingService.isValidAudioFile(file)) {
      alert('Please select a WAV audio file. Other formats may not be supported.')
      return
    }

    try {
      // Process the audio file using our shared service
      const processedAudio = await audioProcessingService.processAudioFile(file)

      // Store uploaded audio separately from recorded audio
      setUploadedAudio(processedAudio)
      recording.clearRecording() // Clear any existing recording

    } catch (error) {
      console.error('Failed to process audio file:', error)
      alert('Failed to process audio file. Please try a different file.')
    }
  }

  // Get the audio data for processing (prioritize uploaded audio, then recorded audio)
  const audioForProcessing = uploadedAudio || recording.processedAudio

  // Handle processing with different modes
  const handleProcessAudio = async (mode: any) => {
    if (!audioForProcessing) return

    try {
      await speakerProcessing.processAudio(audioForProcessing, {
        mode,
        minSpeakers,
        maxSpeakers,
        collar,
        minDurationOff
      })
    } catch (error) {
      console.error('Processing failed:', error)
    }
  }

  if (!user) {
    return (
      <div className="text-center py-12">
        <Users className="h-16 w-16 text-muted mx-auto mb-4" />
        <h3 className="heading-sm mb-2">User Required</h3>
        <p className="text-muted">Please select a user to access inference features.</p>
      </div>
    )
  }

  return (
    <div className="max-w-4xl mx-auto space-y-8">
      {/* Header */}
      <div>
        <h1 className="heading-lg">🎯 Speaker Inference</h1>
        <p className="text-secondary mt-2">
          Upload audio files or record live audio for speaker identification and transcription
        </p>
      </div>

      {/* Audio Input Section */}
      <div className="grid md:grid-cols-2 gap-6">
        {/* File Upload */}
        <div className="card p-6">
          <div className="flex items-center space-x-2 mb-4">
            <Upload className="h-5 w-5 text-secondary" />
            <h4 className="font-medium text-primary">Upload Audio File</h4>
          </div>
          <FileUploader
            onUpload={handleFileUpload}
            accept=".wav,.mp3,.m4a,.webm"
            multiple={false}
            disabled={speakerProcessing.isProcessing || recording.recordingState.isRecording}
          />
          <p className="text-sm text-muted mt-2">
            WAV files recommended for best compatibility
          </p>
        </div>

        {/* Audio Recording */}
        <AudioRecordingControls
          recording={recording}
          disabled={speakerProcessing.isProcessing}
          showQuality={true}
        />
      </div>

      {/* Audio Visualization */}
      {audioForProcessing && (
        <div className="card p-6">
          <h4 className="font-medium text-primary mb-4">🎵 {audioForProcessing.filename}</h4>

          {/* Audio Info */}
          <div className="grid grid-cols-3 gap-4 text-sm mb-4">
            <div>
              <span className="text-muted">Duration:</span>
              <span className="ml-2 font-medium">
                {(audioForProcessing.buffer.duration / 60).toFixed(1)} min
              </span>
            </div>
            <div>
              <span className="text-muted">Sample Rate:</span>
              <span className="ml-2 font-medium">
                {(audioForProcessing.buffer.sampleRate / 1000).toFixed(1)} kHz
              </span>
            </div>
            <div>
              <span className="text-muted">Channels:</span>
              <span className="ml-2 font-medium">{audioForProcessing.buffer.channels}</span>
            </div>
          </div>

          {/* Waveform */}
          <WaveformPlot
            samples={audioForProcessing.buffer.samples}
            sampleRate={audioForProcessing.buffer.sampleRate}
            height={100}
          />

          {/* Audio Quality Info */}
          {audioForProcessing.quality && (
            <div className="mt-4 p-3 card-secondary rounded-lg">
              <div className="flex items-center justify-between text-sm">
                <span className="text-secondary">Audio Quality:</span>
                <span className={`font-medium ${
                  audioForProcessing.quality.level === 'excellent' ? 'text-green-600 dark:text-green-400' :
                  audioForProcessing.quality.level === 'good' ? 'text-blue-600 dark:text-blue-400' :
                  audioForProcessing.quality.level === 'fair' ? 'text-yellow-600 dark:text-yellow-400' :
                  'text-red-600 dark:text-red-400'
                }`}>
                  {audioForProcessing.quality.level.charAt(0).toUpperCase() + audioForProcessing.quality.level.slice(1)}
                  ({audioForProcessing.quality.snr.toFixed(1)} dB SNR)
                </span>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Processing Mode Selection */}
      {audioForProcessing && (
        <ProcessingModeSelector
          selectedMode={speakerProcessing.currentMode}
          onModeChange={speakerProcessing.setProcessingMode}
          onProcessAudio={handleProcessAudio}
          audioData={audioForProcessing}
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
          showSettings={true}
        />
      )}

      {/* Processing Progress */}
      {speakerProcessing.processingProgress && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 dark:bg-blue-900 dark:border-blue-800">
          <div className="flex items-center space-x-2">
            <div className="w-4 h-4 border-2 border-blue-600 border-t-transparent rounded-full animate-spin"></div>
            <span className="text-blue-800 dark:text-blue-200 font-medium">{speakerProcessing.processingProgress}</span>
          </div>
        </div>
      )}

      {/* Results Section */}
      <div className="space-y-6">
        {/* Results History */}
        {speakerProcessing.results.length > 0 && (
          <div className="space-y-4">
            <h2 className="heading-md">📊 Processing History</h2>

            <div className="space-y-3">
              {speakerProcessing.results.map((result) => (
                <div
                  key={result.id}
                  className={`border rounded-lg p-4 cursor-pointer transition-colors ${
                    speakerProcessing.selectedResult?.id === result.id
                      ? 'border-blue-500 bg-blue-50 dark:border-blue-400 dark:bg-blue-900'
                      : 'border-gray-200 hover:border-gray-300 dark:border-gray-700 dark:hover:border-gray-600'
                  }`}
                  onClick={() => speakerProcessing.selectResult(result)}
                >
                  <div className="flex items-center justify-between">
                    <div>
                      <h3 className="font-medium text-primary">{result.filename}</h3>
                      <div className="flex items-center space-x-4 mt-1 text-sm text-muted">
                        <span>{(result.duration / 60).toFixed(1)} min</span>
                        <span>{new Date(result.created_at).toLocaleString()}</span>
                        <span className={`px-2 py-1 rounded-full text-xs font-medium ${
                          result.status === 'completed' ? 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200' :
                          result.status === 'processing' ? 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200' :
                          'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200'
                        }`}>
                          {result.status}
                        </span>
                        <span className="px-2 py-1 rounded-full text-xs bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200">
                          {result.mode}
                        </span>
                      </div>
                    </div>

                    {result.status === 'completed' && (
                      <div className="flex items-center space-x-4">
                        <div className="text-right text-sm">
                          <div className="text-primary font-medium">{result.speakers.length} segments</div>
                          <div className="text-muted">
                            {result.confidence_summary.high_confidence} high conf.
                          </div>
                        </div>
                        <button
                          onClick={(e) => {
                            e.stopPropagation()
                            speakerProcessing.exportResult(result)
                          }}
                          className="p-2 text-green-600 hover:text-green-800 border border-green-200 rounded dark:text-green-400 dark:hover:text-green-300 dark:border-green-700"
                          title="Export Results"
                        >
                          <Upload className="h-4 w-4" />
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Selected Result Details */}
        {speakerProcessing.selectedResult && (
          <SpeakerResultsDisplay
            result={speakerProcessing.selectedResult}
            showTranscription={true}
            showExport={true}
            showStats={true}
            onExport={speakerProcessing.exportResult}
            className="card p-6"
          />
        )}

        {/* No results message */}
        {speakerProcessing.results.length === 0 && !speakerProcessing.isProcessing && (
          <div className="text-center py-12">
            <Users className="h-12 w-12 text-muted mx-auto mb-4" />
            <h3 className="heading-sm mb-2">No Results Yet</h3>
            <p className="text-muted">
              Upload an audio file or record audio to start speaker identification
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
