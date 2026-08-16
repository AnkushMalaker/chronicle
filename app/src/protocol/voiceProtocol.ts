/** Strict protocol-v1 contracts shared with Chronicle's authenticated WebSocket. */

export const VOICE_DUPLEX_PROTOCOL = 1 as const;
export const MAX_RESPONSE_BYTES = 16 * 1024 * 1024;
export const MAX_RESPONSE_DURATION_MS = 60_000;

export type ProcessingProfile =
  | 'ambient'
  | 'imported'
  | 'source_native'
  | 'duplex_aec'
  | 'duplex_isolated'
  | 'half_duplex';

export type VoiceMode = 'duplex_full' | 'duplex_isolated' | 'duplex_half';
export type InputRoute =
  | 'built_in_mic'
  | 'bluetooth_hfp'
  | 'wired_mic'
  | 'usb'
  | 'unknown';
export type OutputRoute =
  | 'speakerphone'
  | 'earpiece'
  | 'headphones'
  | 'bluetooth_hfp'
  | 'usb'
  | 'remote'
  | 'unknown';

export interface EffectStatus {
  requested: boolean;
  available: boolean;
  enabled: boolean;
}

export interface VoiceCapabilities {
  mode: VoiceMode;
  input_route: InputRoute;
  output_route: OutputRoute;
  native_sample_rate: number;
  aec: EffectStatus;
  noise_suppression: EffectStatus;
  fallback_reason:
    | 'aec_unavailable'
    | 'aec_unhealthy'
    | 'route_not_isolated'
    | 'unsupported_route'
    | 'platform_unavailable'
    | null;
}

interface ProtocolEvent {
  protocol: typeof VOICE_DUPLEX_PROTOCOL;
  event_id: string;
  client_id: string;
  sent_at: string;
}

interface BoundVoiceEvent extends ProtocolEvent {
  audio_session_id: string;
  voice_session_id: string;
  capture_epoch: number;
}

export interface AudioSessionStarted extends ProtocolEvent {
  type: 'audio-session.started';
  audio_session_id: string;
  capture_epoch: number;
  processing_profile: ProcessingProfile;
  voice_session_id: string | null;
}

export interface VoiceSessionStart extends BoundVoiceEvent {
  type: 'voice-session.start';
  resume_token: string;
  response_generation: number;
  readiness_deadline_ms: number;
}

export interface VoiceSessionReady extends BoundVoiceEvent {
  type: 'voice-session.ready';
  capabilities: VoiceCapabilities;
}

export interface VoiceSessionCapabilitiesChanged extends BoundVoiceEvent {
  type: 'voice-session.capabilities-changed';
  reason:
    | 'route_changed'
    | 'interruption'
    | 'engine_reset'
    | 'effect_failed'
    | 'audio_focus_lost';
  capabilities: VoiceCapabilities;
}

export interface VoiceSessionResume extends ProtocolEvent {
  type: 'voice-session.resume';
  previous_voice_session_id: string;
  previous_capture_epoch: number;
  resume_token: string;
  last_response_generation: number;
}

export interface VoiceSessionStop extends BoundVoiceEvent {
  type: 'voice-session.stop';
  reason:
    | 'interaction_complete'
    | 'user_requested'
    | 'audio_disconnect'
    | 'temporarily_unavailable';
}

export interface VoiceSessionStopped extends BoundVoiceEvent {
  type: 'voice-session.stopped';
  restoration_succeeded: boolean;
  failure_code:
    | 'far_field_restore_failed'
    | 'permission_denied'
    | 'engine_unavailable'
    | null;
}

export interface ResponseAudio extends BoundVoiceEvent {
  type: 'response.audio';
  turn_id: string;
  turn_revision: number;
  response_id: string;
  generation: number;
  sequence: 0;
  kind: 'speech' | 'tone';
  barge_in_allowed: boolean;
  media_type: 'audio/wav';
  sample_rate: number;
  byte_length: number;
  duration_ms: number;
  payload_length: number;
  trace_id: string;
  causation_id: string;
}

export interface ResponseCancel extends BoundVoiceEvent {
  type: 'response.cancel';
  response_id: string;
  generation: number;
  reason:
    | 'barge_in'
    | 'new_turn'
    | 'replacement'
    | 'route_change'
    | 'disconnect'
    | 'session_stopped';
}

export interface ResponsePlayback extends BoundVoiceEvent {
  type: 'response.playback';
  response_id: string;
  generation: number;
  state: 'started' | 'done' | 'cancelled' | 'failed';
  monotonic_timestamp_ms: number;
  error_code:
    | 'decode_failed'
    | 'route_changed'
    | 'engine_reset'
    | 'playback_unavailable'
    | null;
}

export type ServerVoiceProtocolEvent =
  | AudioSessionStarted
  | VoiceSessionStart
  | VoiceSessionStop
  | ResponseAudio
  | ResponseCancel;

export type PhoneVoiceProtocolEvent =
  | VoiceSessionReady
  | VoiceSessionCapabilitiesChanged
  | VoiceSessionResume
  | VoiceSessionStopped
  | ResponsePlayback;

export type VoiceProtocolEvent = ServerVoiceProtocolEvent | PhoneVoiceProtocolEvent;

const EVENT_TYPES = [
  'audio-session.started',
  'voice-session.start',
  'voice-session.ready',
  'voice-session.capabilities-changed',
  'voice-session.resume',
  'voice-session.stop',
  'voice-session.stopped',
  'response.audio',
  'response.cancel',
  'response.playback',
] as const;

const BASE_KEYS = ['type', 'protocol', 'event_id', 'client_id', 'sent_at'] as const;
const BINDING_KEYS = ['audio_session_id', 'voice_session_id', 'capture_epoch'] as const;

function expectRecord(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function expectString(value: unknown, label: string, minimumLength = 1): string {
  if (typeof value !== 'string' || value.length < minimumLength) {
    throw new Error(`${label} must be a non-empty string`);
  }
  return value;
}

function expectInteger(value: unknown, label: string, minimum = 0): number {
  if (!Number.isInteger(value) || (value as number) < minimum) {
    throw new Error(`${label} must be an integer >= ${minimum}`);
  }
  return value as number;
}

function expectBoolean(value: unknown, label: string): boolean {
  if (typeof value !== 'boolean') throw new Error(`${label} must be boolean`);
  return value;
}

function expectOneOf<T extends string>(
  value: unknown,
  choices: readonly T[],
  label: string,
): T {
  if (typeof value !== 'string' || !choices.includes(value as T)) {
    throw new Error(`${label} is unsupported`);
  }
  return value as T;
}

function expectNullableOneOf<T extends string>(
  value: unknown,
  choices: readonly T[],
  label: string,
): T | null {
  if (value === null) return null;
  return expectOneOf(value, choices, label);
}

function expectExactKeys(record: Record<string, unknown>, allowed: readonly string[]) {
  for (const key of Object.keys(record)) {
    if (!allowed.includes(key)) throw new Error(`unknown protocol field: ${key}`);
  }
  for (const key of allowed) {
    if (!(key in record)) throw new Error(`missing protocol field: ${key}`);
  }
}

function validateBase(record: Record<string, unknown>) {
  if (record.protocol !== VOICE_DUPLEX_PROTOCOL) {
    throw new Error('unsupported voice duplex protocol');
  }
  expectOneOf(record.type, EVENT_TYPES, 'type');
  const eventId = expectString(record.event_id, 'event_id');
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(eventId)) {
    throw new Error('event_id must be a UUID');
  }
  expectString(record.client_id, 'client_id');
  const sentAt = expectString(record.sent_at, 'sent_at');
  if (!/(?:Z|[+-]\d\d:\d\d)$/.test(sentAt) || Number.isNaN(Date.parse(sentAt))) {
    throw new Error('sent_at must include a valid UTC offset');
  }
}

function validateBinding(record: Record<string, unknown>) {
  expectString(record.audio_session_id, 'audio_session_id');
  expectString(record.voice_session_id, 'voice_session_id');
  expectInteger(record.capture_epoch, 'capture_epoch');
}

function validateEffect(value: unknown, label: string): EffectStatus {
  const effect = expectRecord(value, label);
  expectExactKeys(effect, ['requested', 'available', 'enabled']);
  const requested = expectBoolean(effect.requested, `${label}.requested`);
  const available = expectBoolean(effect.available, `${label}.available`);
  const enabled = expectBoolean(effect.enabled, `${label}.enabled`);
  if (enabled && (!requested || !available)) {
    throw new Error(`${label} cannot be enabled unless requested and available`);
  }
  return { requested, available, enabled };
}

function validateCapabilities(value: unknown): VoiceCapabilities {
  const capabilities = expectRecord(value, 'capabilities');
  expectExactKeys(capabilities, [
    'mode',
    'input_route',
    'output_route',
    'native_sample_rate',
    'aec',
    'noise_suppression',
    'fallback_reason',
  ]);
  const mode = expectOneOf(
    capabilities.mode,
    ['duplex_full', 'duplex_isolated', 'duplex_half'] as const,
    'capabilities.mode',
  );
  const inputRoute = expectOneOf(
    capabilities.input_route,
    ['built_in_mic', 'bluetooth_hfp', 'wired_mic', 'usb', 'unknown'] as const,
    'capabilities.input_route',
  );
  const outputRoute = expectOneOf(
    capabilities.output_route,
    ['speakerphone', 'earpiece', 'headphones', 'bluetooth_hfp', 'usb', 'remote', 'unknown'] as const,
    'capabilities.output_route',
  );
  const nativeSampleRate = expectInteger(
    capabilities.native_sample_rate,
    'capabilities.native_sample_rate',
    1,
  );
  const aec = validateEffect(capabilities.aec, 'capabilities.aec');
  const noiseSuppression = validateEffect(
    capabilities.noise_suppression,
    'capabilities.noise_suppression',
  );
  const fallbackReason = expectNullableOneOf(
    capabilities.fallback_reason,
    [
      'aec_unavailable',
      'aec_unhealthy',
      'route_not_isolated',
      'unsupported_route',
      'platform_unavailable',
    ] as const,
    'capabilities.fallback_reason',
  );
  if (mode === 'duplex_full' && (outputRoute !== 'speakerphone' || !aec.enabled)) {
    throw new Error('duplex_full requires speakerphone with enabled AEC');
  }
  if (
    mode === 'duplex_isolated' &&
    !(['headphones', 'bluetooth_hfp', 'usb'] as const).includes(
      outputRoute as 'headphones' | 'bluetooth_hfp' | 'usb',
    )
  ) {
    throw new Error('duplex_isolated requires isolated output');
  }
  if ((mode === 'duplex_half') !== (fallbackReason !== null)) {
    throw new Error('fallback_reason must match duplex_half mode');
  }
  return {
    mode,
    input_route: inputRoute,
    output_route: outputRoute,
    native_sample_rate: nativeSampleRate,
    aec,
    noise_suppression: noiseSuppression,
    fallback_reason: fallbackReason,
  };
}

/** Parse an untrusted JSON control header and reject aliases or unknown fields. */
export function parseVoiceProtocolEvent(value: unknown): VoiceProtocolEvent {
  const event = expectRecord(value, 'voice protocol event');
  validateBase(event);
  const type = event.type as VoiceProtocolEvent['type'];
  const extraKeys: string[] = [];

  switch (type) {
    case 'audio-session.started':
      extraKeys.push('audio_session_id', 'capture_epoch', 'processing_profile', 'voice_session_id');
      expectString(event.audio_session_id, 'audio_session_id');
      expectInteger(event.capture_epoch, 'capture_epoch');
      expectOneOf(
        event.processing_profile,
        ['ambient', 'imported', 'source_native', 'duplex_aec', 'duplex_isolated', 'half_duplex'] as const,
        'processing_profile',
      );
      if (event.voice_session_id !== null) expectString(event.voice_session_id, 'voice_session_id');
      break;
    case 'voice-session.resume':
      extraKeys.push(
        'previous_voice_session_id',
        'previous_capture_epoch',
        'resume_token',
        'last_response_generation',
      );
      expectString(event.previous_voice_session_id, 'previous_voice_session_id');
      expectInteger(event.previous_capture_epoch, 'previous_capture_epoch');
      expectString(event.resume_token, 'resume_token', 32);
      expectInteger(event.last_response_generation, 'last_response_generation');
      break;
    default:
      extraKeys.push(...BINDING_KEYS);
      validateBinding(event);
  }

  switch (type) {
    case 'voice-session.start':
      extraKeys.push('resume_token', 'response_generation', 'readiness_deadline_ms');
      expectString(event.resume_token, 'resume_token', 32);
      expectInteger(event.response_generation, 'response_generation');
      expectInteger(event.readiness_deadline_ms, 'readiness_deadline_ms', 100);
      break;
    case 'voice-session.ready':
      extraKeys.push('capabilities');
      validateCapabilities(event.capabilities);
      break;
    case 'voice-session.capabilities-changed':
      extraKeys.push('reason', 'capabilities');
      expectOneOf(
        event.reason,
        ['route_changed', 'interruption', 'engine_reset', 'effect_failed', 'audio_focus_lost'] as const,
        'reason',
      );
      validateCapabilities(event.capabilities);
      break;
    case 'voice-session.stop':
      extraKeys.push('reason');
      expectOneOf(
        event.reason,
        ['interaction_complete', 'user_requested', 'audio_disconnect', 'temporarily_unavailable'] as const,
        'reason',
      );
      break;
    case 'voice-session.stopped': {
      extraKeys.push('restoration_succeeded', 'failure_code');
      const succeeded = expectBoolean(event.restoration_succeeded, 'restoration_succeeded');
      const failure = expectNullableOneOf(
        event.failure_code,
        ['far_field_restore_failed', 'permission_denied', 'engine_unavailable'] as const,
        'failure_code',
      );
      if (succeeded === (failure !== null)) throw new Error('failure_code must match restoration result');
      break;
    }
    case 'response.audio': {
      extraKeys.push(
        'turn_id',
        'turn_revision',
        'response_id',
        'generation',
        'sequence',
        'kind',
        'barge_in_allowed',
        'media_type',
        'sample_rate',
        'byte_length',
        'duration_ms',
        'payload_length',
        'trace_id',
        'causation_id',
      );
      expectString(event.turn_id, 'turn_id');
      expectInteger(event.turn_revision, 'turn_revision');
      expectString(event.response_id, 'response_id');
      expectInteger(event.generation, 'generation');
      if (event.sequence !== 0) throw new Error('version one supports sequence zero only');
      const kind = expectOneOf(event.kind, ['speech', 'tone'] as const, 'kind');
      const bargeInAllowed = expectBoolean(event.barge_in_allowed, 'barge_in_allowed');
      if (kind === 'tone' && bargeInAllowed) throw new Error('tones cannot allow barge-in');
      if (event.media_type !== 'audio/wav') throw new Error('media_type must be audio/wav');
      expectInteger(event.sample_rate, 'sample_rate', 1);
      const bytes = expectInteger(event.byte_length, 'byte_length', 1);
      const duration = expectInteger(event.duration_ms, 'duration_ms', 1);
      const payloadLength = expectInteger(event.payload_length, 'payload_length', 1);
      if (bytes > MAX_RESPONSE_BYTES || payloadLength !== bytes) throw new Error('invalid WAV byte length');
      if (duration > MAX_RESPONSE_DURATION_MS) throw new Error('response duration exceeds limit');
      expectString(event.trace_id, 'trace_id');
      expectString(event.causation_id, 'causation_id');
      break;
    }
    case 'response.cancel':
      extraKeys.push('response_id', 'generation', 'reason');
      expectString(event.response_id, 'response_id');
      expectInteger(event.generation, 'generation');
      expectOneOf(
        event.reason,
        ['barge_in', 'new_turn', 'replacement', 'route_change', 'disconnect', 'session_stopped'] as const,
        'reason',
      );
      break;
    case 'response.playback': {
      extraKeys.push('response_id', 'generation', 'state', 'monotonic_timestamp_ms', 'error_code');
      expectString(event.response_id, 'response_id');
      expectInteger(event.generation, 'generation');
      const state = expectOneOf(
        event.state,
        ['started', 'done', 'cancelled', 'failed'] as const,
        'state',
      );
      expectInteger(event.monotonic_timestamp_ms, 'monotonic_timestamp_ms');
      const error = expectNullableOneOf(
        event.error_code,
        ['decode_failed', 'route_changed', 'engine_reset', 'playback_unavailable'] as const,
        'error_code',
      );
      if ((state === 'failed') !== (error !== null)) throw new Error('error_code must match failed state');
      break;
    }
    case 'audio-session.started':
    case 'voice-session.resume':
      break;
  }

  expectExactKeys(event, [...BASE_KEYS, ...extraKeys]);
  return event as unknown as VoiceProtocolEvent;
}
