// @vitest-environment jsdom

import { act, render } from '@testing-library/react'
import { useEffect } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { decodeMediaEnvelope } from '../protocol/audioV2'
import { RecordingProvider, type RecordingContextType, useRecording } from './RecordingContext'

vi.mock('./AuthContext', () => ({ useAuth: () => ({ user: { id: 'user-1' } }) }))
vi.mock('../services/api', () => ({ BACKEND_URL: '' }))
vi.mock('../hooks/useWakeFeedback', () => ({ setActiveWakeClientId: vi.fn() }))

class FakeWebSocket {
  static OPEN = 1
  static instances: FakeWebSocket[] = []
  readyState = FakeWebSocket.OPEN
  binaryType: BinaryType = 'blob'
  sent: unknown[] = []
  onopen: (() => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null

  constructor(public url: string, public protocols?: string | string[]) {
    FakeWebSocket.instances.push(this)
    queueMicrotask(() => this.onopen?.())
  }

  send(value: unknown) {
    this.sent.push(value)
    if (typeof value !== 'string') return
    const control = JSON.parse(value)
    const envelope = {
      event_id: { value: crypto.randomUUID() },
      sent_at: new Date().toISOString(),
    }
    if (control.hello) {
      queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({
        ...envelope,
        hello: { client_id: { value: 'client-1' }, connection_id: { value: 'connection-1' } },
      }) } as MessageEvent))
    } else if (control.start_capture) {
      queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({
        ...envelope,
        capture_started: {
          binding: {
            capture_session_id: { value: 'capture-1' },
            voice_session_id: { value: '' },
            capture_epoch: control.start_capture.capture_epoch,
          },
          audio_spec: control.start_capture.audio_spec,
        },
      }) } as MessageEvent))
    } else if (control.stop_capture) {
      queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({
        ...envelope,
        capture_stopped: { binding: control.stop_capture.binding },
      }) } as MessageEvent))
    }
  }
  close() { this.readyState = 3 }
}

class FakeAudioContext {
  sampleRate = 16000
  currentTime = 1
  state: AudioContextState = 'running'
  destination = {} as AudioDestinationNode
  processor: { onaudioprocess: ((event: AudioProcessingEvent) => void) | null } | null = null
  processorBufferSize: number | null = null

  createAnalyser() {
    return {
      fftSize: 0,
      connect: vi.fn(),
      disconnect: vi.fn(),
      getFloatTimeDomainData: vi.fn(),
    } as unknown as AnalyserNode
  }
  createMediaStreamSource() {
    return { connect: vi.fn(), disconnect: vi.fn() } as unknown as MediaStreamAudioSourceNode
  }
  createScriptProcessor(size: number) {
    this.processorBufferSize = size
    this.processor = { onaudioprocess: null }
    return {
      get onaudioprocess() { return thisState.processor?.onaudioprocess ?? null },
      set onaudioprocess(value) { if (thisState.processor) thisState.processor.onaudioprocess = value },
      connect: vi.fn(),
      disconnect: vi.fn(),
    } as unknown as ScriptProcessorNode
  }
  resume = vi.fn(async () => undefined)
  close = vi.fn(async () => undefined)
}

let thisState: FakeAudioContext
let recording: RecordingContextType | null = null
let microphoneTrackStop: ReturnType<typeof vi.fn>

function Harness() {
  const value = useRecording()
  useEffect(() => { recording = value }, [value])
  return null
}

describe('RecordingProvider audio V2 interface', () => {
  beforeEach(() => {
    recording = null
    FakeWebSocket.instances = []
    localStorage.clear()
    localStorage.setItem('root_token', 'test-token')
    thisState = new FakeAudioContext()
    microphoneTrackStop = vi.fn()
    vi.stubGlobal('WebSocket', FakeWebSocket)
    vi.stubGlobal('AudioData', class {
      constructor(_options: unknown) {}
      close() {}
    })
    vi.stubGlobal('AudioEncoder', class {
      private output: (chunk: unknown) => void
      constructor(options: { output: (chunk: unknown) => void }) { this.output = options.output }
      configure() {}
      encode() {
        this.output({
          byteLength: 4,
          copyTo: (target: Uint8Array) => target.set([1, 2, 3, 4]),
        })
      }
      async flush() {}
      close() {}
    })
    vi.stubGlobal('AudioContext', function AudioContext() { return thisState })
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: {
        enumerateDevices: vi.fn(async () => []),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        getUserMedia: vi.fn(async () => ({
          getTracks: () => [{ stop: microphoneTrackStop }],
          getAudioTracks: () => [{}],
        })),
      },
    })
  })

  it('restores the last selected microphone and clears it with System Default', () => {
    localStorage.setItem('root_microphoneDeviceId', 'usb-microphone')

    render(<RecordingProvider><Harness /></RecordingProvider>)

    expect(recording!.selectedDeviceId).toBe('usb-microphone')

    act(() => recording!.setSelectedDeviceId('desk-microphone'))

    expect(localStorage.getItem('root_microphoneDeviceId')).toBe('desk-microphone')

    act(() => recording!.setSelectedDeviceId(null))

    expect(recording!.selectedDeviceId).toBeNull()
    expect(localStorage.getItem('root_microphoneDeviceId')).toBeNull()
  })

  it('encodes microphone input into atomic raw-Opus V2 packets', async () => {
    render(<RecordingProvider><Harness /></RecordingProvider>)
    await act(async () => { await recording!.startRecording() })

    const input = new Float32Array(512)
    await act(async () => {
      thisState.processor!.onaudioprocess!({
        playbackTime: 1.25,
        inputBuffer: { getChannelData: () => input },
      } as unknown as AudioProcessingEvent)
    })

    const socket = FakeWebSocket.instances[0]
    expect(thisState.processorBufferSize).toBe(1024)
    const headers = socket.sent
      .filter((value): value is string => typeof value === 'string')
      .map(value => JSON.parse(value))
    expect(headers.some(value => value.hello)).toBe(true)
    expect(headers.some(value => value.start_capture)).toBe(true)
    const packets = socket.sent.filter((value): value is Uint8Array => value instanceof Uint8Array)
    expect(packets).toHaveLength(1)
    const envelope = decodeMediaEnvelope(packets[0]!)
    expect(envelope.media.case).toBe('capture')
    if (envelope.media.case !== 'capture') throw new Error('expected capture packet')
    expect(envelope.media.value.opusPayload).toEqual(new Uint8Array([1, 2, 3, 4]))
    expect(socket.protocols).toBe('chronicle.audio.v2')
    expect(socket.readyState).toBe(FakeWebSocket.OPEN)
  })

  it('stops capture through the bound V2 control before closing transport', async () => {
    render(<RecordingProvider><Harness /></RecordingProvider>)
    await act(async () => { await recording!.startRecording() })

    const socket = FakeWebSocket.instances[0]
    vi.useFakeTimers()
    act(() => recording!.stopRecording())

    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    const headers = socket.sent
      .filter((value): value is string => typeof value === 'string')
      .map(value => JSON.parse(value))
    expect(headers.some(value => value.stop_capture)).toBe(true)
    expect(microphoneTrackStop).toHaveBeenCalledOnce()
    expect(thisState.close).toHaveBeenCalledOnce()
    expect(socket.readyState).toBe(3)
    vi.useRealTimers()
  })
})
