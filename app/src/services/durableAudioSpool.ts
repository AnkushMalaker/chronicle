import AsyncStorage from '@react-native-async-storage/async-storage';
import { Directory, File, Paths } from 'expo-file-system';
import type { FileHandle } from 'expo-file-system';

const SEGMENT_MS = 30_000;
const HEADER_BYTES = 16;
const ACK_PREFIX = 'chronicle.audioSpool.ack.';

export type AudioSpoolSource = 'phone' | 'wearable';

export interface SpoolPacket {
  fileName: string;
  /**
   * Identity of the spool *file* this packet was written to, not the backend audio
   * session. It was called `sessionId` and sent as `durable_session_id`, which the
   * backend echoed back as `session_id` — three names for a spool segment, all of
   * them colliding with the real WebSocket SessionId that means something else.
   */
  segmentId: string;
  sequence: number;
  capturedAtMs: number;
  payload: Uint8Array;
}

interface ActiveSegment {
  file: File;
  handle: FileHandle;
  source: AudioSpoolSource;
  segmentId: string;
  startedAtMs: number;
  nextSequence: number;
}

const makeSegmentId = (): string =>
  `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;

/**
 * Append-only, document-directory audio spool.
 *
 * Every captured packet reaches a source-tagged segment before it is offered to the
 * WebSocket. Files remain until the backend accepts their decoded PCM into Redis.
 * Phone and wearable packets never share a recovery capture because their declared
 * Opus durations differ.
 */
export class DurableAudioSpool {
  private readonly directory = new Directory(Paths.document, 'chronicle-audio-spool');
  private active: ActiveSegment | null = null;
  private readonly acknowledgmentChains = new Map<string, Promise<void>>();

  private ensureDirectory(): void {
    if (!this.directory.exists) {
      this.directory.create({ idempotent: true, intermediates: true });
    }
  }

  private closeActive(): void {
    if (!this.active) return;
    this.active.handle.close();
    this.active = null;
  }

  private startSegment(source: AudioSpoolSource, capturedAtMs: number): ActiveSegment {
    this.ensureDirectory();
    const segmentId = makeSegmentId();
    const file = new File(this.directory, `${source}-${segmentId}.spool`);
    file.create({ overwrite: false, intermediates: true });
    const active = {
      file,
      handle: file.open(),
      source,
      segmentId,
      startedAtMs: capturedAtMs,
      nextSequence: 0,
    };
    this.active = active;
    return active;
  }

  append(
    source: AudioSpoolSource,
    payload: Uint8Array,
    capturedAtMs = Date.now(),
  ): SpoolPacket {
    let segment = this.active;
    if (
      !segment ||
      segment.source !== source ||
      capturedAtMs - segment.startedAtMs >= SEGMENT_MS
    ) {
      this.closeActive();
      segment = this.startSegment(source, capturedAtMs);
    }

    const sequence = segment.nextSequence++;
    const frame = new Uint8Array(HEADER_BYTES + payload.length);
    const view = new DataView(frame.buffer);
    view.setUint32(0, sequence, false);
    view.setFloat64(4, capturedAtMs, false);
    view.setUint32(12, payload.length, false);
    frame.set(payload, HEADER_BYTES);
    segment.handle.writeBytes(frame);

    return {
      fileName: segment.file.name,
      segmentId: segment.segmentId,
      sequence,
      capturedAtMs,
      payload,
    };
  }

  async pendingPackets(source: AudioSpoolSource): Promise<SpoolPacket[]> {
    this.ensureDirectory();
    const packets: SpoolPacket[] = [];
    const files = this.directory
      .list()
      .filter((entry): entry is File => (
        entry instanceof File && entry.name.startsWith(`${source}-`) && entry.name.endsWith('.spool')
      ));

    for (const file of files) {
      await this.acknowledgmentChains.get(file.name)?.catch(() => undefined);
      const segmentId = file.name.slice(0, -'.spool'.length);
      const acknowledged = Number(await AsyncStorage.getItem(`${ACK_PREFIX}${file.name}`) ?? '-1');
      const bytes = file.bytesSync();
      let offset = 0;
      let finalSequence = -1;
      while (offset + HEADER_BYTES <= bytes.length) {
        const view = new DataView(bytes.buffer, bytes.byteOffset + offset, HEADER_BYTES);
        const sequence = view.getUint32(0, false);
        const capturedAtMs = view.getFloat64(4, false);
        const length = view.getUint32(12, false);
        const end = offset + HEADER_BYTES + length;
        if (end > bytes.length) break; // Ignore a final partial frame after a hard crash.
        finalSequence = sequence;
        if (sequence > acknowledged) {
          packets.push({
            fileName: file.name,
            segmentId,
            sequence,
            capturedAtMs,
            payload: bytes.slice(offset + HEADER_BYTES, end),
          });
        }
        offset = end;
      }
      if (
        finalSequence >= 0 &&
        acknowledged >= finalSequence &&
        this.active?.file.name !== file.name
      ) {
        file.delete();
        await AsyncStorage.removeItem(`${ACK_PREFIX}${file.name}`);
      }
    }
    return packets.sort((a, b) => a.capturedAtMs - b.capturedAtMs);
  }

  private async acknowledgeInOrder(packet: SpoolPacket): Promise<void> {
    const ackKey = `${ACK_PREFIX}${packet.fileName}`;
    const isActive = this.active?.file.name === packet.fileName;
    const file = isActive ? null : new File(this.directory, packet.fileName);
    if (file && !file.exists) {
      // A higher ACK may already have retired this closed segment. Do not let a
      // later, lower ACK recreate its watermark after the file is gone.
      await AsyncStorage.removeItem(ackKey);
      return;
    }

    const previous = Number(await AsyncStorage.getItem(ackKey) ?? '-1');
    const acknowledged = Math.max(previous, packet.sequence);
    await AsyncStorage.setItem(ackKey, String(acknowledged));
    if (isActive) return;

    const bytes = file!.bytesSync();
    let offset = 0;
    let finalSequence = -1;
    while (offset + HEADER_BYTES <= bytes.length) {
      const view = new DataView(bytes.buffer, bytes.byteOffset + offset, HEADER_BYTES);
      const sequence = view.getUint32(0, false);
      const length = view.getUint32(12, false);
      const end = offset + HEADER_BYTES + length;
      if (end > bytes.length) break;
      finalSequence = sequence;
      offset = end;
    }
    if (finalSequence >= 0 && acknowledged >= finalSequence) {
      file!.delete();
      await AsyncStorage.removeItem(ackKey);
    }
  }

  async acknowledge(packet: SpoolPacket): Promise<void> {
    const previous = this.acknowledgmentChains.get(packet.fileName) ?? Promise.resolve();
    const operation = previous
      // A transient storage failure must be reported to its own caller, but it
      // must not permanently poison retirement for every later ACK in the file.
      .catch(() => undefined)
      .then(() => this.acknowledgeInOrder(packet));
    this.acknowledgmentChains.set(packet.fileName, operation);
    try {
      await operation;
    } finally {
      if (this.acknowledgmentChains.get(packet.fileName) === operation) {
        this.acknowledgmentChains.delete(packet.fileName);
      }
    }
  }

  close(): void {
    this.closeActive();
  }
}

export const durableAudioSpool = new DurableAudioSpool();
