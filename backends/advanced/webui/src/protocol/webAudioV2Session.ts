import { create } from '@bufbuild/protobuf'
import { DurationSchema } from '@bufbuild/protobuf/wkt'

import {
  AudioCodec,
  AudioSpecSchema,
  CaptureBindingSchema,
  CaptureMediaPacketSchema,
  CaptureSourceIdSchema,
  ClientControlSchema,
  ClientHelloSchema,
  DataPurpose,
  DeliveryClass,
  DeviceKind,
  EventIdSchema,
  MediaEnvelopeSchema,
  MemorySpaceIdSchema,
  ProcessingProfile,
  StartCaptureSchema,
  StopCaptureSchema,
  StopReason,
  decodeServerControl,
  encodeClientControl,
  encodeMediaEnvelope,
  timestampFromUnixMs,
  type CaptureBinding,
} from './audioV2'

const FRAME_SAMPLES = 320
const FRAME_DURATION_US = 20_000
const CONTROL_TIMEOUT_MS = 10_000

interface ControlWaiter {
  resolve: (value: any) => void
  reject: (error: Error) => void
  timeout: ReturnType<typeof setTimeout>
}

function id() {
  return create(EventIdSchema, { value: crypto.randomUUID() })
}

function spec() {
  return create(AudioSpecSchema, {
    codec: AudioCodec.OPUS,
    sampleRateHz: 16_000,
    channelCount: 1,
    frameDuration: create(DurationSchema, { nanos: 20_000_000 }),
    bitrateBps: 24_000,
  })
}

export class WebAudioV2Session {
  private socket: WebSocket | null = null
  private binding: CaptureBinding | null = null
  private encoder: any = null
  private pending = new Float32Array(0)
  private sequence = 0
  private frameTimestampUs = 0
  private capturedAtOriginMs = 0
  private waiters = new Map<string, ControlWaiter>()
  private fatalErrorReported = false
  private closingNormally = false

  constructor(
    private readonly url: string,
    private readonly bearerToken: string,
    private readonly onClientId: (clientId: string) => void,
    private readonly onTranscript: (text: string, isFinal: boolean) => void,
    private readonly onFatalError: (error: Error) => void,
  ) {}

  async connect(): Promise<void> {
    const AudioEncoderCtor = (globalThis as any).AudioEncoder
    const AudioDataCtor = (globalThis as any).AudioData
    if (!AudioEncoderCtor || !AudioDataCtor) {
      throw new Error('This browser does not provide the WebCodecs Opus encoder')
    }
    const socket = new WebSocket(this.url, 'chronicle.audio.v2')
    this.socket = socket
    const hello = this.waitFor('hello')
    socket.onmessage = event => {
      if (typeof event.data !== 'string') return
      try {
        const control = decodeServerControl(event.data)
        const kind = control.event.case
        if (kind === 'error') {
          this.fail(new Error(control.event.value.detail || 'Audio V2 server rejected the request'))
          return
        }
        if (kind === 'hello') this.onClientId(control.event.value.clientId?.value ?? '')
        if (kind === 'transcriptUpdate') {
          const update = control.event.value
          if (update.text) this.onTranscript(update.text, update.isFinal)
        }
        const waiter = kind === undefined ? undefined : this.waiters.get(kind)
        if (waiter && kind !== undefined) {
          clearTimeout(waiter.timeout)
          this.waiters.delete(kind)
          waiter.resolve(control)
        }
      } catch (error) {
        this.fail(error instanceof Error ? error : new Error('Invalid Audio V2 server control'))
      }
    }
    await new Promise<void>((resolve, reject) => {
      socket.onopen = () => resolve()
      socket.onerror = () => {
        const error = new Error('Audio V2 WebSocket failed')
        this.fail(error)
        reject(error)
      }
      socket.onclose = event => {
        if (this.closingNormally && event.code === 1000) return
        const suffix = event.reason ? `: ${event.reason}` : ''
        const error = new Error(`Audio V2 WebSocket closed${suffix}`)
        this.fail(error)
        reject(error)
      }
    })
    this.send('hello', create(ClientHelloSchema, {
      bearerToken: this.bearerToken,
      sourceId: create(CaptureSourceIdSchema, { value: 'webui-recorder' }),
      deviceKind: DeviceKind.WEB_BROWSER,
      displayName: 'webui-recorder',
      supportedUplink: [spec()],
    }))
    await hello
  }

  async start(memorySpaceId?: string): Promise<void> {
    const started = this.waitFor('captureStarted')
    this.send('startCapture', create(StartCaptureSchema, {
      // SOURCE_NATIVE is a direct capture stream, not a recoverable phone spool.
      // Its backend provenance invariant requires epoch zero.
      captureEpoch: 0n,
      processingProfile: ProcessingProfile.SOURCE_NATIVE,
      dataPurpose: DataPurpose.NORMAL_CAPTURE,
      deliveryClass: DeliveryClass.LIVE,
      audioSpec: spec(),
      memorySpaceId: memorySpaceId
        ? create(MemorySpaceIdSchema, { value: memorySpaceId })
        : undefined,
    }))
    const control = await started
    this.binding = control.event.value.binding
    this.sequence = 0
    this.frameTimestampUs = 0
    this.capturedAtOriginMs = Date.now()
    const Encoder = (globalThis as any).AudioEncoder
    this.encoder = new Encoder({
      output: (chunk: any) => this.sendEncoded(chunk),
      error: (error: Error) => { throw error },
    })
    this.encoder.configure({
      codec: 'opus',
      sampleRate: 16_000,
      numberOfChannels: 1,
      bitrate: 24_000,
      opus: { frameDuration: FRAME_DURATION_US },
    })
  }

  push(samples: Float32Array): void {
    if (!this.encoder || !this.binding) return
    const joined = new Float32Array(this.pending.length + samples.length)
    joined.set(this.pending)
    joined.set(samples, this.pending.length)
    let offset = 0
    const AudioDataCtor = (globalThis as any).AudioData
    while (joined.length - offset >= FRAME_SAMPLES) {
      const frame = joined.slice(offset, offset + FRAME_SAMPLES)
      const data = new AudioDataCtor({
        format: 'f32-planar',
        sampleRate: 16_000,
        numberOfFrames: FRAME_SAMPLES,
        numberOfChannels: 1,
        timestamp: this.frameTimestampUs,
        data: frame,
      })
      this.encoder.encode(data)
      data.close()
      this.frameTimestampUs += FRAME_DURATION_US
      offset += FRAME_SAMPLES
    }
    this.pending = joined.slice(offset)
  }

  async stop(): Promise<void> {
    if (!this.socket) return
    if (this.encoder) {
      await this.encoder.flush()
      this.encoder.close()
      this.encoder = null
    }
    if (this.binding) {
      const stopped = this.waitFor('captureStopped')
      this.send('stopCapture', create(StopCaptureSchema, {
        binding: this.binding,
        reason: StopReason.USER_REQUESTED,
      }))
      await stopped
    }
    this.closingNormally = true
    this.socket.close(1000, 'capture-complete')
    this.socket = null
    this.binding = null
    this.pending = new Float32Array(0)
  }

  private sendEncoded(chunk: any): void {
    if (!this.binding || !this.socket || this.socket.readyState !== WebSocket.OPEN) return
    const payload = new Uint8Array(chunk.byteLength)
    chunk.copyTo(payload)
    const sequence = this.sequence++
    this.socket.send(encodeMediaEnvelope(create(MediaEnvelopeSchema, {
      media: {
        case: 'capture',
        value: create(CaptureMediaPacketSchema, {
          binding: create(CaptureBindingSchema, this.binding),
          sequence: BigInt(sequence),
          capturedAt: timestampFromUnixMs(this.capturedAtOriginMs + sequence * 20),
          monotonicOffsetUs: BigInt(sequence * FRAME_DURATION_US),
          deliveryClass: DeliveryClass.LIVE,
          opusPayload: payload,
        }),
      },
    })))
  }

  private send(caseName: any, value: any): void {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      throw new Error('Audio V2 WebSocket is not open')
    }
    this.socket.send(encodeClientControl(create(ClientControlSchema, {
      eventId: id(),
      sentAt: timestampFromUnixMs(Date.now()),
      event: { case: caseName, value } as any,
    })))
  }

  private waitFor(kind: string): Promise<any> {
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.waiters.delete(kind)
        reject(new Error(`Timed out waiting for Audio V2 ${kind}`))
      }, CONTROL_TIMEOUT_MS)
      this.waiters.set(kind, { resolve, reject, timeout })
    })
  }

  private rejectAll(error: Error): void {
    for (const [kind, waiter] of this.waiters) {
      clearTimeout(waiter.timeout)
      waiter.reject(new Error(`${error.message} before ${kind}`))
    }
    this.waiters.clear()
  }

  private fail(error: Error): void {
    this.rejectAll(error)
    if (this.fatalErrorReported) return
    this.fatalErrorReported = true
    this.onFatalError(error)
  }
}
