import { useState } from 'react'
import { Radio, Zap, Archive, Settings, Monitor, Mic } from 'lucide-react'
import { useRecording, isLoopbackDevice, isMacOS } from '../contexts/RecordingContext'
import { Button } from '../components/ui'
import SimplifiedControls from '../components/audio/SimplifiedControls'
import StatusDisplay from '../components/audio/StatusDisplay'
import AudioVisualizer from '../components/audio/AudioVisualizer'
import SimpleDebugPanel from '../components/audio/SimpleDebugPanel'
import WakeFeedback from '../components/audio/WakeFeedback'

export default function LiveRecord({
  memorySpaceId,
  destinationLabel = 'Main',
  embedded = false,
}: {
  memorySpaceId?: string
  destinationLabel?: string
  embedded?: boolean
} = {}) {
  const recording = useRecording()
  const [isLoadingMicrophones, setIsLoadingMicrophones] = useState(false)
  const microphoneDevices = recording.availableDevices.filter(
    device => recording.audioSource === 'mic' || !isLoopbackDevice(device.label)
  )
  const microphoneLabelsKnown = microphoneDevices.some(device => device.label)

  const loadMicrophones = async () => {
    setIsLoadingMicrophones(true)
    try {
      await recording.requestDeviceAccess()
    } finally {
      setIsLoadingMicrophones(false)
    }
  }

  return (
    <div>
      {/* Header */}
      <div className={`flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between ${embedded ? 'mb-4' : 'mb-6'}`}>
        <div className="flex items-center space-x-2">
          <Radio className="h-6 w-6 text-blue-600 flex-shrink-0" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            {embedded ? `Record into ${destinationLabel}` : 'Live Audio Recording'}
          </h1>
        </div>

        {/* Mode Toggle */}
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={() => recording.setMode('streaming')}
            disabled={recording.isRecording}
            className={`
              flex items-center gap-2 px-4 py-2 rounded-lg font-medium transition-all
              ${recording.mode === 'streaming'
                ? 'bg-blue-600 text-white shadow-md'
                : 'bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-400 hover:bg-gray-300 dark:hover:bg-gray-600'
              }
              ${recording.isRecording ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
            `}
          >
            <Zap className="h-4 w-4" />
            <span>Streaming</span>
          </button>
          <button
            onClick={() => recording.setMode('batch')}
            disabled={recording.isRecording}
            className={`
              flex items-center gap-2 px-4 py-2 rounded-lg font-medium transition-all
              ${recording.mode === 'batch'
                ? 'bg-blue-600 text-white shadow-md'
                : 'bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-400 hover:bg-gray-300 dark:hover:bg-gray-600'
              }
              ${recording.isRecording ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
            `}
          >
            <Archive className="h-4 w-4" />
            <span>Batch</span>
          </button>
        </div>
      </div>

      {/* Audio Source Toggle */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className={`inline-flex rounded-lg border border-gray-300 dark:border-gray-600 bg-gray-100 dark:bg-gray-800 p-0.5 ${recording.isRecording ? 'opacity-50 pointer-events-none' : ''}`}>
          <button
            onClick={() => recording.setAudioSource('mic')}
            disabled={recording.isRecording}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium transition-all ${
              recording.audioSource === 'mic'
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200'
            }`}
          >
            <Mic className="h-3.5 w-3.5" />
            <span>Mic</span>
          </button>
          <button
            onClick={() => recording.setAudioSource('meeting')}
            disabled={recording.isRecording}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium transition-all ${
              recording.audioSource === 'meeting'
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200'
            }`}
          >
            <Mic className="h-3.5 w-3.5" />
            <Monitor className="h-3.5 w-3.5" />
            <span>Meeting</span>
          </button>
          <button
            onClick={() => recording.setAudioSource('tab')}
            disabled={recording.isRecording}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium transition-all ${
              recording.audioSource === 'tab'
                ? 'bg-blue-600 text-white shadow-sm'
                : 'text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200'
            }`}
          >
            <Monitor className="h-3.5 w-3.5" />
            <span>Tab</span>
          </button>
        </div>
        <span className="text-sm text-gray-500 dark:text-gray-400">
          {recording.audioSource === 'mic'
            ? 'Microphone only'
            : recording.audioSource === 'meeting'
              ? recording.monitorDeviceId
                ? 'Mic + system audio (from the loopback device below)'
                : 'Mic + tab audio (you\'ll be asked to select a tab)'
              : recording.monitorDeviceId
                ? 'System audio only (from the loopback device below)'
                : 'Browser tab audio only (no microphone)'}
        </span>
      </div>

      {/* Optional loopback capture. Chromium shares tab audio straight from the picker,
          so this stays hidden there; Firefox ignores `audio: true` (bugzilla #1541425) and
          needs a loopback input — a PipeWire/PulseAudio monitor on Linux, or a driver the
          user installed on macOS, which has no built-in equivalent. Choosing one here
          skips the share picker entirely and captures the whole output instead of one tab. */}
      {recording.audioSource !== 'mic' && (() => {
        // Labels are blank until an audio permission is granted, so an empty list
        // means "not probed yet" — not "this machine has no loopback device".
        const labelsKnown = recording.availableDevices.some(d => d.label)
        const loopbacks = recording.availableDevices.filter(d => isLoopbackDevice(d.label))
        // A macOS Aggregate Device can be named anything, so once we know the labels
        // and found no known driver, offer every input rather than a dead end.
        const options = loopbacks.length > 0
          ? loopbacks
          : (isMacOS && labelsKnown ? recording.availableDevices : [])
        // On Chromium the share picker handles this, so stay out of the way unless
        // the browser needs a loopback or the user already has one to choose from.
        const relevant = recording.likelyLacksDisplayAudio || recording.monitorDeviceId || loopbacks.length > 0
        if (!relevant) return null
        return (
          <div className="mb-4 space-y-2">
            <div className="flex items-center gap-2">
              <Monitor className="h-4 w-4 text-gray-500 dark:text-gray-400 flex-shrink-0" />
              <label className="text-sm font-medium text-gray-700 dark:text-gray-300 flex-shrink-0">
                System audio:
              </label>
              {options.length > 0 ? (
                <select
                  value={recording.monitorDeviceId ?? ''}
                  onChange={(e) => recording.setMonitorDeviceId(e.target.value || null)}
                  disabled={recording.isRecording}
                  className={`
                    flex-1 min-w-0 text-sm px-2 py-1.5 rounded-lg border
                    bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100
                    border-gray-300 dark:border-gray-600
                    ${recording.isRecording ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
                  `}
                >
                  <option value="">
                    {isMacOS ? 'Choose loopback input (e.g. BlackHole)' : 'Choose "Monitor of …" output device'}
                  </option>
                  {options.map((device) => (
                    <option key={device.deviceId} value={device.deviceId}>
                      {device.label}
                    </option>
                  ))}
                </select>
              ) : labelsKnown ? (
                <span className="text-sm text-orange-600 dark:text-orange-400">
                  No loopback input found on this machine.
                </span>
              ) : (
                <Button variant="secondary" size="sm" onClick={() => recording.requestDeviceAccess()}>
                  Load audio devices…
                </Button>
              )}
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              {recording.monitorDeviceId
                ? 'Selected — the share picker will be skipped and everything playing through this output is recorded. Clear it to share a single tab instead.'
                : 'Leave unset to pick a browser tab from the share dialog instead.'}{' '}
              {recording.likelyLacksDisplayAudio && (
                <>
                  {isMacOS ? (
                    <>
                      Firefox can't share tab audio (bugzilla #1541425) and macOS has no built-in loopback input, so
                      this needs a virtual audio driver. <strong>Simplest fix: use a Chromium browser</strong>, where
                      tab audio works with no setup. To stay in Firefox: <code>brew install --cask blackhole-2ch</code>,
                      build a Multi-Output Device in Audio MIDI Setup so you can still hear the meeting, then select
                      BlackHole above.
                    </>
                  ) : (
                    <>
                      Firefox can't share tab audio, so pick the "Monitor of …" entry for the output you're actually
                      listening through — headphones and speakers each have their own.
                    </>
                  )}{' '}
                  If browser capture still doesn't work, use the Chronicle tray's ScreenPipe recorder instead; it
                  captures the meeting outside Firefox.
                </>
              )}
            </p>
          </div>
        )
      })()}

      {/* Microphone setup stays visible before recording. Browsers hide device labels
          until permission is granted, so the first action probes and immediately
          releases the default input; it does not start a Chronicle recording. */}
      {recording.audioSource !== 'tab' && (
        <div className="mb-4 space-y-1.5">
          <div className="flex items-center gap-2">
            <Settings className="h-4 w-4 text-gray-500 dark:text-gray-400 flex-shrink-0" />
            <label
              htmlFor="recording-microphone"
              className="text-sm font-medium text-gray-700 dark:text-gray-300 flex-shrink-0"
            >
              Microphone:
            </label>
            {microphoneLabelsKnown ? (
              <select
                id="recording-microphone"
                value={recording.selectedDeviceId ?? ''}
                onChange={(e) => recording.setSelectedDeviceId(e.target.value || null)}
                disabled={recording.isRecording}
                className={`
                  flex-1 min-w-0 text-sm px-2 py-1.5 rounded-lg border
                  bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100
                  border-gray-300 dark:border-gray-600
                  ${recording.isRecording ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}
                `}
              >
                <option value="">System Default</option>
                {microphoneDevices.map((device) => (
                  <option key={device.deviceId} value={device.deviceId}>
                    {device.label}
                  </option>
                ))}
              </select>
            ) : (
              <Button
                variant="secondary"
                size="sm"
                onClick={loadMicrophones}
                disabled={recording.isRecording || isLoadingMicrophones}
              >
                {isLoadingMicrophones ? 'Loading microphones…' : 'Choose microphone…'}
              </Button>
            )}
          </div>
          {!microphoneLabelsKnown && (
            <p className="pl-6 text-xs text-gray-500 dark:text-gray-400">
              Your browser will ask for microphone access so Chronicle can list devices. Recording will not start.
            </p>
          )}
        </div>
      )}

      {/* Mode Description */}
      <div className="mb-4 bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-3">
        <p className="text-sm text-gray-700 dark:text-gray-300">
          {recording.mode === 'streaming' ? (
            <>
              <strong>Streaming Mode:</strong> Audio is sent in real-time chunks and processed immediately.
              Transcription starts while you're still speaking.
            </>
          ) : (
            <>
              <strong>Batch Mode:</strong> Audio is accumulated and sent as a complete file when you stop recording.
              Transcription begins after recording ends.
            </>
          )}
        </p>
      </div>

      {/* Main Controls - Single START button */}
      <div className="mb-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/30 dark:text-red-200">
        Recording destination: <strong>{destinationLabel}</strong>
      </div>
      <SimplifiedControls recording={recording} memorySpaceId={memorySpaceId} />

      {/* System-audio capture health (meeting/tab mode) */}
      {recording.isRecording && recording.audioSource !== 'mic' && (
        <div className={`mb-6 -mt-2 p-3 rounded-lg border text-sm ${
          recording.systemAudioStatus === 'silent'
            ? 'bg-orange-50 dark:bg-orange-900/20 border-orange-200 dark:border-orange-800 text-orange-700 dark:text-orange-300'
            : 'bg-gray-50 dark:bg-gray-800 border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-400'
        }`}>
          <span className="font-medium">System audio:</span>{' '}
          {recording.systemAudioLabel ?? 'not captured'}
          {recording.systemAudioStatus === 'active' && (
            <span className="text-green-600 dark:text-green-400"> — receiving audio ✓</span>
          )}
          {recording.systemAudioStatus === 'silent' && (
            <span>
              {' '}— <strong>no signal detected.</strong> If something is playing, this is the wrong capture
              device: pick the "Monitor of …" entry matching the output you're actually listening through
              (headphones vs speakers each have their own monitor), then restart the recording.
            </span>
          )}
        </div>
      )}

      {/* Status Display - Shows setup progress */}
      <StatusDisplay recording={recording} />

      {/* Audio Visualizer - Shows waveform when recording */}
      <AudioVisualizer
        isRecording={recording.isRecording}
        analyser={recording.analyser}
      />

      {/* Live streaming transcript - real-time text from the streaming STT provider */}
      {(recording.isRecording || recording.liveTranscript) && (
        <div className="mt-4 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
          <div className="flex items-center gap-2 mb-2">
            <span className="relative flex h-2.5 w-2.5">
              {recording.isRecording && (
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"></span>
              )}
              <span className={`relative inline-flex rounded-full h-2.5 w-2.5 ${recording.isRecording ? 'bg-green-500' : 'bg-gray-400'}`}></span>
            </span>
            <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300">
              Live Transcript
            </h3>
            <span className="text-xs text-gray-400">(streaming)</span>
          </div>
          <p className="text-gray-900 dark:text-gray-100 whitespace-pre-wrap min-h-[1.5rem]">
            {recording.liveTranscript || (
              <span className="text-gray-400 italic">
                {recording.isRecording ? 'Listening…' : ''}
              </span>
            )}
          </p>
        </div>
      )}

      {/* Wake-word feedback - pulses on arm/end-of-turn + shows recognized command */}
      <WakeFeedback />

      {/* Instructions */}
      <div className="mt-8 bg-gray-50 dark:bg-gray-800/50 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
        <h3 className="font-medium text-gray-700 dark:text-gray-200 mb-2">
          📝 How it Works
        </h3>
        <ul className="text-sm text-gray-600 dark:text-gray-400 space-y-1">
          <li>• <strong>Choose your mode:</strong> Streaming for real-time or Batch for complete file processing</li>
          <li>• <strong>One-click recording:</strong> Single button handles complete setup automatically</li>
          <li>• <strong>Sequential process:</strong> Mic access → WebSocket connection → Audio session → Recording</li>
          <li>• <strong>Mode-based processing:</strong>
            {recording.mode === 'streaming'
              ? 'Real-time chunks sent as you speak'
              : 'Complete audio sent after you stop'
            }
          </li>
          <li>• <strong>Audio v2:</strong> Every Opus packet carries its session binding, clock, and sequence</li>
          <li>• <strong>Efficient audio:</strong> 16kHz mono Opus with noise suppression and echo cancellation</li>
          <li>• <strong>View results:</strong> Check Conversations page for transcribed content and memories</li>
        </ul>
      </div>

      {/* Debug Information Panel */}
      <SimpleDebugPanel recording={recording} />
    </div>
  )
}
