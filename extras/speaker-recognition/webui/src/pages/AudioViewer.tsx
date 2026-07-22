import { useState, useRef, useCallback, useEffect } from 'react'
import { Play, Pause, Download } from 'lucide-react'
import { useUser } from '../contexts/UserContext'
import { calculateFileHash, isAudioFile } from '../utils/fileHash'
import {
  loadAudioBuffer,
  createAudioContext,
  decodeAudioData,
  extractAudioSamples,
  calculateSNR,
  formatDuration,
  createAudioBlob
} from '../utils/audioUtils'
import WaveformPlot from '../components/WaveformPlot'
import FileUploader from '../components/FileUploader'

interface AudioData {
  file: File
  buffer: AudioBuffer
  hash: string
  samples: Float32Array
  snr: number
}

export default function AudioViewer() {
  const { user } = useUser()
  const [audioData, setAudioData] = useState<AudioData | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [showSpectrogram, setShowSpectrogram] = useState(false)
  const [isPlaying, setIsPlaying] = useState(false)
  const [playbackPosition, setPlaybackPosition] = useState<number | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const audioSourceRef = useRef<AudioBufferSourceNode | null>(null)
  const playbackStartTimeRef = useRef<number>(0)

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      // Stop any playing audio when component unmounts
      if (audioSourceRef.current) {
        try {
          audioSourceRef.current.stop()
          audioSourceRef.current.disconnect()
        } catch (e) {
          // Ignore errors if already stopped
        }
      }

      // Close audio context
      if (audioContextRef.current && audioContextRef.current.state !== 'closed') {
        audioContextRef.current.close()
      }
    }
  }, [])

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

      // Load and decode audio
      const arrayBuffer = await loadAudioBuffer(file)
      const audioContext = createAudioContext()
      audioContextRef.current = audioContext

      const audioBuffer = await decodeAudioData(audioContext, arrayBuffer)
      const samples = extractAudioSamples(audioBuffer)

      // Analyze audio
      const snr = calculateSNR(samples)

      setAudioData({
        file,
        buffer: audioBuffer,
        hash,
        samples,
        snr
      })
    } catch (error) {
      console.error('Audio processing error details:', error)

      let errorMessage = 'Failed to process audio file. '
      if (error instanceof Error) {
        if (error.message.includes('decode') || error.name === 'EncodingError') {
          errorMessage += 'The audio format may not be supported or the file may be corrupted. Try converting to WAV format.'
        } else if (error.message.includes('buffer') || error.message.includes('read')) {
          errorMessage += 'Failed to read the file. Please check the file is not corrupted.'
        } else if (error.message.includes('stack') || error.name === 'RangeError') {
          errorMessage += 'The file is too large to process. Please try a smaller file or shorter audio clip.'
        } else if (error.message.includes('memory') || error.message.includes('allocation')) {
          errorMessage += 'Insufficient memory to process this file. Try a smaller file or refresh the page.'
        } else {
          errorMessage += `Error: ${error.message}`
        }
      } else {
        errorMessage += 'Unknown error occurred. Please try a different file.'
      }

      alert(errorMessage)
    } finally {
      setIsLoading(false)
    }
  }, [user])

  const playAudio = useCallback(async (startTime: number = 0) => {
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

      source.start(0, startTime)
      audioSourceRef.current = source
      setIsPlaying(true)
      setPlaybackPosition(startTime)
      playbackStartTimeRef.current = audioContextRef.current.currentTime - startTime

      source.onended = () => {
        setIsPlaying(false)
        setPlaybackPosition(null)
        audioSourceRef.current = null
      }
    } catch (error) {
      console.error('Failed to play audio:', error)
      setIsPlaying(false)
      setPlaybackPosition(null)
    }
  }, [audioData])

  const stopAudio = useCallback(() => {
    if (audioSourceRef.current) {
      audioSourceRef.current.stop()
      audioSourceRef.current = null
      setIsPlaying(false)
      setPlaybackPosition(null)
    }
  }, [])

  const togglePlayback = useCallback(() => {
    if (isPlaying) {
      stopAudio()
    } else {
      playAudio(0)
    }
  }, [isPlaying, stopAudio, playAudio])

  const handleWaveformClick = useCallback((time: number) => {
    playAudio(time)
  }, [playAudio])


  const exportFullAudio = useCallback(() => {
    if (!audioData) return

    const blob = createAudioBlob(audioData.samples, audioData.buffer.sampleRate)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${audioData.file.name.split('.')[0]}_processed.wav`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }, [audioData])

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
        <h1 className="heading-lg">🎵 Audio Viewer</h1>
        {audioData && (
          <div className="flex space-x-2">
            <button
              onClick={() => togglePlayback()}
              className="flex items-center space-x-2 px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 dark:bg-blue-700 dark:hover:bg-blue-800"
            >
              {isPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
              <span>{isPlaying ? 'Stop' : 'Play All'}</span>
            </button>
            <button
              onClick={exportFullAudio}
              className="flex items-center space-x-2 px-4 py-2 bg-green-600 text-white rounded-md hover:bg-green-700 dark:bg-green-700 dark:hover:bg-green-800"
            >
              <Download className="h-4 w-4" />
              <span>Export</span>
            </button>
          </div>
        )}
      </div>

      <p className="text-secondary">
        Upload and explore audio files with interactive visualization and segment selection.
      </p>

      {/* File Upload */}
      <div className="border-2 border-dashed border-gray-300 rounded-lg p-6 dark:border-gray-600">
        <FileUploader
          onUpload={handleFileUpload}
          accept=".wav,.flac,.mp3,.m4a,.ogg"
          multiple={false}
          disabled={isLoading}
        />
        {isLoading && (
          <div className="text-center mt-4">
            <div className="inline-block animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600 dark:border-blue-400"></div>
            <p className="mt-2 text-sm text-secondary">Processing audio file...</p>
          </div>
        )}
      </div>

      {audioData && (
        <>
          {/* Audio Information */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <div className="card-secondary p-4">
              <div className="text-sm font-medium text-muted">Duration</div>
              <div className="heading-lg">
                {formatDuration(audioData.buffer.duration)}
              </div>
            </div>
            <div className="card-secondary p-4">
              <div className="text-sm font-medium text-muted">Sample Rate</div>
              <div className="heading-lg">
                {(audioData.buffer.sampleRate / 1000).toFixed(1)} kHz
              </div>
            </div>
            <div className="card-secondary p-4">
              <div className="text-sm font-medium text-muted">Channels</div>
              <div className="heading-lg">
                {audioData.buffer.numberOfChannels}
              </div>
            </div>
            <div className="card-secondary p-4">
              <div className="text-sm font-medium text-muted">SNR</div>
              <div className="heading-lg">
                {audioData.snr.toFixed(1)} dB
              </div>
            </div>
          </div>

          {/* Waveform Visualization */}
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="heading-sm">Waveform</h3>
              <label className="flex items-center space-x-2">
                <input
                  type="checkbox"
                  checked={showSpectrogram}
                  onChange={(e) => setShowSpectrogram(e.target.checked)}
                  className="rounded border-gray-300 dark:border-gray-600"
                />
                <span className="text-sm input-label">Show Spectrogram</span>
              </label>
            </div>

            <WaveformPlot
              samples={audioData.samples}
              sampleRate={audioData.buffer.sampleRate}
              showSpectrogram={showSpectrogram}
              onTimeClick={handleWaveformClick}
              playbackPosition={playbackPosition}
              isPlaying={isPlaying}
            />
          </div>

          {/* Click Instructions */}
          <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 dark:bg-blue-900 dark:border-blue-800">
            <h4 className="font-medium text-blue-800 dark:text-blue-200 mb-2">Interactive Playback</h4>
            <p className="text-sm text-blue-700 dark:text-blue-300">
              Click anywhere on the waveform to play from that position. Click the "Play All" button to play from the beginning.
            </p>
          </div>
        </>
      )}
    </div>
  )
}
