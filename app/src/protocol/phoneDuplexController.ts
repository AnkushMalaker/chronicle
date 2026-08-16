import type {
  NativePlaybackState,
  NativeResponse,
  NativeRouteChange,
  NativeStopResult,
} from '../../modules/chronicle-duplex-audio';
import {
  parseVoiceProtocolEvent,
  type AudioSessionStarted,
  type PhoneVoiceProtocolEvent,
  type ResponseAudio,
  type VoiceCapabilities,
  type VoiceSessionStart,
  VOICE_DUPLEX_PROTOCOL,
} from './voiceProtocol';

interface NativeDuplexPort {
  scheduleResponse(response: NativeResponse): Promise<void>;
  cancelResponse(responseId: string, generation: number): Promise<void>;
  stopVoiceSession(): Promise<NativeStopResult>;
}

export interface PhoneCaptureBinding {
  captureEpoch: number;
  capabilities: VoiceCapabilities;
}

export interface PhoneResumeProof {
  previousVoiceSessionId: string;
  previousCaptureEpoch: number;
  resumeToken: string;
  lastResponseGeneration: number;
}

export interface PhoneDuplexControllerOptions {
  capabilities: VoiceCapabilities;
  captureEpoch: number;
  native: NativeDuplexPort;
  send: (event: PhoneVoiceProtocolEvent) => Promise<void> | void;
  restartCapture?: () => Promise<PhoneCaptureBinding>;
  replaceAudioSession?: (
    binding: PhoneCaptureBinding,
    voiceSessionId: string
  ) => Promise<void>;
  resumeProof?: PhoneResumeProof | null;
  createEventId?: () => string;
  now?: () => Date;
}

function randomUuid(): string {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (token) => {
    const value = Math.floor(Math.random() * 16);
    const nibble = token === 'x' ? value : (value & 0x3) | 0x8;
    return nibble.toString(16);
  });
}

function toBase64(bytes: Uint8Array): string {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  let encoded = '';
  for (let offset = 0; offset < bytes.length; offset += 3) {
    const first = bytes[offset];
    const second = offset + 1 < bytes.length ? bytes[offset + 1] : 0;
    const third = offset + 2 < bytes.length ? bytes[offset + 2] : 0;
    const value = (first << 16) | (second << 8) | third;
    encoded += alphabet[(value >> 18) & 63];
    encoded += alphabet[(value >> 12) & 63];
    encoded += offset + 1 < bytes.length ? alphabet[(value >> 6) & 63] : '=';
    encoded += offset + 2 < bytes.length ? alphabet[value & 63] : '=';
  }
  return encoded;
}

export class PhoneDuplexController {
  private capabilities: VoiceCapabilities;
  private captureEpoch: number;
  private readonly native: NativeDuplexPort;
  private readonly sendEvent: PhoneDuplexControllerOptions['send'];
  private readonly restartCapture?: PhoneDuplexControllerOptions['restartCapture'];
  private readonly replaceAudioSession?: PhoneDuplexControllerOptions['replaceAudioSession'];
  private readonly createEventId: () => string;
  private readonly now: () => Date;
  private audioSession: AudioSessionStarted | null = null;
  private voiceSession: VoiceSessionStart | null = null;
  private resumeState: PhoneResumeProof | null;
  private routeTransition: NativeRouteChange | null = null;
  private lastResponseGeneration = 0;
  private pendingAudio: ResponseAudio | null = null;
  private seenEvents = new Set<string>();
  private closed = false;

  constructor(options: PhoneDuplexControllerOptions) {
    this.capabilities = options.capabilities;
    this.captureEpoch = options.captureEpoch;
    this.native = options.native;
    this.sendEvent = options.send;
    this.restartCapture = options.restartCapture;
    this.replaceAudioSession = options.replaceAudioSession;
    this.resumeState = options.resumeProof ?? null;
    this.createEventId = options.createEventId ?? randomUuid;
    this.now = options.now ?? (() => new Date());
  }

  get protocolHandshakeComplete(): boolean {
    return this.audioSession !== null;
  }

  get resumeProof(): PhoneResumeProof | null {
    if (this.voiceSession) {
      return {
        previousVoiceSessionId: this.voiceSession.voice_session_id,
        previousCaptureEpoch: this.voiceSession.capture_epoch,
        resumeToken: this.voiceSession.resume_token,
        lastResponseGeneration: this.lastResponseGeneration,
      };
    }
    return this.resumeState;
  }

  async receiveControl(value: unknown): Promise<void> {
    if (this.closed) return;
    const event = parseVoiceProtocolEvent(value);
    if (this.seenEvents.has(event.event_id)) return;
    this.seenEvents.add(event.event_id);
    if (this.seenEvents.size > 1_024) {
      this.seenEvents = new Set(Array.from(this.seenEvents).slice(-512));
    }

    switch (event.type) {
      case 'audio-session.started':
        if (event.capture_epoch !== this.captureEpoch) {
          throw new Error('stale audio-session.started capture epoch');
        }
        this.audioSession = event;
        if (this.resumeState) {
          if (event.voice_session_id !== this.resumeState.previousVoiceSessionId) {
            throw new Error('resumed audio session lost its prior voice binding');
          }
          await this.sendUnbound('voice-session.resume', {
            previous_voice_session_id: this.resumeState.previousVoiceSessionId,
            previous_capture_epoch: this.resumeState.previousCaptureEpoch,
            resume_token: this.resumeState.resumeToken,
            last_response_generation: this.resumeState.lastResponseGeneration,
          });
        } else if (this.routeTransition && this.voiceSession) {
          if (event.voice_session_id !== this.voiceSession.voice_session_id) {
            throw new Error('replacement capture lost its voice-session binding');
          }
          this.voiceSession = {
            ...this.voiceSession,
            audio_session_id: event.audio_session_id,
            capture_epoch: event.capture_epoch,
            response_generation: this.lastResponseGeneration,
          };
          const transition = this.routeTransition;
          this.routeTransition = null;
          await this.send('voice-session.capabilities-changed', {
            reason: transition.reason,
            capabilities: this.capabilities,
          });
        }
        return;
      case 'voice-session.start':
        this.assertAudioBinding(event);
        this.voiceSession = event;
        this.resumeState = null;
        this.lastResponseGeneration = event.response_generation;
        await this.send('voice-session.ready', { capabilities: this.capabilities });
        return;
      case 'response.audio':
        this.assertVoiceBinding(event);
        if (event.generation < this.lastResponseGeneration) {
          throw new Error('stale response generation');
        }
        this.lastResponseGeneration = event.generation;
        if (this.pendingAudio) throw new Error('response payload already pending');
        this.pendingAudio = event;
        return;
      case 'response.cancel':
        this.assertVoiceBinding(event);
        if (event.generation < this.lastResponseGeneration) {
          throw new Error('stale cancellation generation');
        }
        this.lastResponseGeneration = event.generation;
        if (this.pendingAudio?.response_id === event.response_id) this.pendingAudio = null;
        await this.native.cancelResponse(event.response_id, event.generation);
        return;
      case 'voice-session.stop': {
        this.assertVoiceBinding(event);
        this.pendingAudio = null;
        const stopped = await this.native.stopVoiceSession();
        await this.send('voice-session.stopped', {
          restoration_succeeded: stopped.restorationSucceeded,
          failure_code: stopped.failureCode,
        });
        this.voiceSession = null;
        this.resumeState = null;
        return;
      }
      default:
        throw new Error(`server sent invalid phone-bound event ${event.type}`);
    }
  }

  async receiveBinary(value: ArrayBuffer | Uint8Array): Promise<void> {
    if (this.closed) return;
    const header = this.pendingAudio;
    if (!header) throw new Error('binary response arrived without response.audio');
    this.pendingAudio = null;
    this.assertVoiceBinding(header);
    const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
    if (bytes.byteLength !== header.byte_length) {
      throw new Error('binary response length does not match response.audio');
    }
    await this.native.scheduleResponse({
      responseId: header.response_id,
      generation: header.generation,
      captureEpoch: header.capture_epoch,
      wavBase64: toBase64(bytes),
    });
  }

  async nativePlaybackChanged(state: NativePlaybackState): Promise<void> {
    if (this.closed || !this.voiceSession) return;
    if (state.captureEpoch !== this.captureEpoch) return;
    await this.send('response.playback', {
      response_id: state.responseId,
      generation: state.generation,
      state: state.state,
      monotonic_timestamp_ms: Math.round(state.monotonicTimestampMs),
      error_code: state.state === 'failed' ? state.errorCode ?? 'playback_unavailable' : null,
    });
  }

  async nativeRouteChanged(change: NativeRouteChange): Promise<void> {
    if (this.closed || !this.voiceSession || change.captureEpoch !== this.captureEpoch) return;
    if (this.routeTransition) return;
    if (!this.restartCapture || !this.replaceAudioSession) {
      throw new Error('capture-session replacement is unavailable');
    }
    this.pendingAudio = null;
    this.routeTransition = change;
    this.lastResponseGeneration += 1;
    try {
      const binding = await this.restartCapture();
      if (binding.captureEpoch <= this.captureEpoch) {
        throw new Error('capture transition did not advance the epoch');
      }
      this.captureEpoch = binding.captureEpoch;
      this.capabilities = binding.capabilities;
      this.audioSession = null;
      await this.replaceAudioSession(binding, this.voiceSession.voice_session_id);
    } catch (cause) {
      this.routeTransition = null;
      throw cause;
    }
  }

  async stopNativeSession(): Promise<void> {
    if (this.closed || !this.voiceSession) return;
    this.pendingAudio = null;
    const stopped = await this.native.stopVoiceSession();
    await this.send('voice-session.stopped', {
      restoration_succeeded: stopped.restorationSucceeded,
      failure_code: stopped.failureCode,
    });
    this.voiceSession = null;
    this.resumeState = null;
  }

  prepareFreshCapture(binding: PhoneCaptureBinding): void {
    if (this.closed || this.voiceSession) {
      throw new Error('cannot replace an active voice session without stopping it');
    }
    if (binding.captureEpoch <= this.captureEpoch) {
      throw new Error('fresh capture did not advance the epoch');
    }
    this.captureEpoch = binding.captureEpoch;
    this.capabilities = binding.capabilities;
    this.audioSession = null;
    this.resumeState = null;
    this.pendingAudio = null;
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    this.pendingAudio = null;
    if (this.voiceSession) {
      await this.native.cancelResponse('*', this.lastResponseGeneration);
    }
  }

  private assertAudioBinding(event: VoiceSessionStart): void {
    const audio = this.audioSession;
    if (
      !audio ||
      event.client_id !== audio.client_id ||
      event.audio_session_id !== audio.audio_session_id ||
      event.capture_epoch !== audio.capture_epoch
    ) {
      throw new Error('voice session does not match authenticated audio binding');
    }
  }

  private assertVoiceBinding(event: {
    client_id: string;
    audio_session_id: string;
    voice_session_id: string;
    capture_epoch: number;
  }): void {
    const voice = this.voiceSession;
    if (
      !voice ||
      event.client_id !== voice.client_id ||
      event.audio_session_id !== voice.audio_session_id ||
      event.voice_session_id !== voice.voice_session_id ||
      event.capture_epoch !== voice.capture_epoch
    ) {
      throw new Error('response does not match active voice binding');
    }
  }

  private async send(
    type: PhoneVoiceProtocolEvent['type'],
    fields: Record<string, unknown>
  ): Promise<void> {
    const voice = this.voiceSession;
    if (!voice) throw new Error(`cannot send ${type} without a voice session`);
    const event = parseVoiceProtocolEvent({
      type,
      protocol: VOICE_DUPLEX_PROTOCOL,
      event_id: this.createEventId(),
      client_id: voice.client_id,
      audio_session_id: voice.audio_session_id,
      voice_session_id: voice.voice_session_id,
      capture_epoch: voice.capture_epoch,
      sent_at: this.now().toISOString(),
      ...fields,
    }) as PhoneVoiceProtocolEvent;
    await this.sendEvent(event);
  }

  private async sendUnbound(
    type: 'voice-session.resume',
    fields: Record<string, unknown>
  ): Promise<void> {
    const audio = this.audioSession;
    if (!audio) throw new Error(`cannot send ${type} without an audio session`);
    const event = parseVoiceProtocolEvent({
      type,
      protocol: VOICE_DUPLEX_PROTOCOL,
      event_id: this.createEventId(),
      client_id: audio.client_id,
      sent_at: this.now().toISOString(),
      ...fields,
    }) as PhoneVoiceProtocolEvent;
    await this.sendEvent(event);
  }
}
