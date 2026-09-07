import {
  create,
  fromBinary,
  fromJsonString,
  toBinary,
  toJsonString,
} from '@bufbuild/protobuf';
import { TimestampSchema, type Timestamp } from '@bufbuild/protobuf/wkt';

import {
  ClientControlSchema,
  MediaEnvelopeSchema,
  ServerControlSchema,
  type ClientControl,
  type MediaEnvelope,
  type ServerControl,
} from './backend/audio_contract/v2/audio_pb';

const MAX_CONTROL_BYTES = 64 * 1024;
const MAX_MEDIA_BYTES = 4 * 1024;

export function timestampFromUnixMs(value: number): Timestamp {
  if (!Number.isFinite(value) || value < 0) {
    throw new Error('timestamp must be a non-negative finite number');
  }
  const seconds = Math.floor(value / 1000);
  return create(TimestampSchema, {
    seconds: BigInt(seconds),
    nanos: Math.floor((value - seconds * 1000) * 1_000_000),
  });
}

export function encodeClientControl(message: ClientControl): string {
  if (message.event.case === undefined || !message.eventId?.value || !message.sentAt) {
    throw new Error('ClientControl requires event_id, sent_at, and one event');
  }
  const encoded = toJsonString(ClientControlSchema, message, {
    useProtoFieldName: true,
  });
  if (new TextEncoder().encode(encoded).length > MAX_CONTROL_BYTES) {
    throw new Error('ClientControl exceeds the wire size limit');
  }
  return encoded;
}

export function decodeServerControl(payload: string): ServerControl {
  if (!payload || new TextEncoder().encode(payload).length > MAX_CONTROL_BYTES) {
    throw new Error('ServerControl has an invalid wire size');
  }
  const message = fromJsonString(ServerControlSchema, payload, {
    ignoreUnknownFields: false,
    recursionLimit: 32,
  });
  if (message.event.case === undefined || !message.eventId?.value || !message.sentAt) {
    throw new Error('ServerControl requires event_id, sent_at, and one event');
  }
  return message;
}

export function encodeMediaEnvelope(message: MediaEnvelope): Uint8Array {
  if (message.media.case === undefined) throw new Error('MediaEnvelope requires media');
  const encoded = toBinary(MediaEnvelopeSchema, message);
  if (!encoded.length || encoded.length > MAX_MEDIA_BYTES) {
    throw new Error('MediaEnvelope has an invalid wire size');
  }
  return encoded;
}

export function decodeMediaEnvelope(payload: Uint8Array): MediaEnvelope {
  if (!payload.length || payload.length > MAX_MEDIA_BYTES) {
    throw new Error('MediaEnvelope has an invalid wire size');
  }
  const message = fromBinary(MediaEnvelopeSchema, payload, {
    readUnknownFields: false,
  });
  if (message.media.case === undefined) throw new Error('MediaEnvelope requires media');
  return message;
}
