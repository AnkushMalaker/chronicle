import type { SpoolPacket } from './durableAudioSpool';

type PacketSender = (packets: SpoolPacket[]) => Promise<void>;

interface DurableAudioDeliveryOptions {
  captureStartedAtMs: number;
  sendLive: PacketSender;
  sendBacklog: PacketSender;
}

interface BacklogSocketEvent {
  data: unknown;
}

interface BacklogSocketCloseEvent {
  code?: number;
  reason?: string;
}

interface BacklogSocket {
  readyState: number;
  onopen: (() => void | Promise<void>) | null;
  onmessage: ((event: BacklogSocketEvent) => void | Promise<void>) | null;
  onerror: ((event: unknown) => void) | null;
  onclose: ((event: BacklogSocketCloseEvent) => void) | null;
  send(data: string | Uint8Array): void;
  close(code?: number, reason?: string): void;
}

interface DurableBacklogUploadOptions {
  url: string;
  audioFormat: Record<string, unknown>;
  createSocket: (url: string) => BacklogSocket;
  acknowledge: (packet: SpoolPacket) => Promise<void>;
}

const packetKey = (packet: Pick<SpoolPacket, 'segmentId' | 'sequence'>): string =>
  `${packet.segmentId}:${packet.sequence}`;

const wyomingHeader = (
  type: string,
  data: Record<string, unknown>,
  payloadLength: number | null = null,
): string => `${JSON.stringify({ type, data, version: '1.0.0', payload_length: payloadLength })}\n`;

/**
 * Routes durable packets without coupling live capture to historical recovery.
 *
 * Packets from the active capture always use the live adapter. Recovered packets
 * that predate the active capture use the backlog adapter, which may remain busy
 * without delaying the live lane.
 */
export class DurableAudioDeliveryCoordinator {
  private readonly captureStartedAtMs: number;
  private readonly sendLive: PacketSender;
  private readonly sendBacklog: PacketSender;

  constructor(options: DurableAudioDeliveryOptions) {
    this.captureStartedAtMs = options.captureStartedAtMs;
    this.sendLive = options.sendLive;
    this.sendBacklog = options.sendBacklog;
  }

  captured(packet: SpoolPacket): Promise<void> {
    return this.sendLive([packet]);
  }

  async recover(packets: SpoolPacket[]): Promise<void> {
    const live: SpoolPacket[] = [];
    const backlog: SpoolPacket[] = [];
    for (const packet of packets) {
      if (packet.capturedAtMs >= this.captureStartedAtMs) {
        live.push(packet);
      } else {
        backlog.push(packet);
      }
    }

    await Promise.all([
      live.length ? this.sendLive(live) : Promise.resolve(),
      backlog.length ? this.sendBacklog(backlog) : Promise.resolve(),
    ]);
  }

  finishCapture(packets: SpoolPacket[]): Promise<void> {
    return packets.length ? this.sendBacklog(packets) : Promise.resolve();
  }
}

/** Uploads historical durable packets on a transport isolated from live capture. */
export class DurableBacklogUploadSession {
  private readonly options: DurableBacklogUploadOptions;

  constructor(options: DurableBacklogUploadOptions) {
    this.options = options;
  }

  upload(packets: SpoolPacket[]): Promise<void> {
    if (!packets.length) return Promise.resolve();

    const pending = new Map(packets.map((packet) => [packetKey(packet), packet]));
    const socket = this.options.createSocket(this.options.url);

    return new Promise<void>((resolve, reject) => {
      let settled = false;
      const fail = (cause: unknown) => {
        if (settled) return;
        settled = true;
        reject(cause instanceof Error ? cause : new Error(String(cause)));
      };

      socket.onopen = () => {
        socket.send(wyomingHeader('audio-start', this.options.audioFormat));
        for (const packet of packets) {
          socket.send(wyomingHeader(
            'audio-chunk',
            {
              ...this.options.audioFormat,
              spool_segment_id: packet.segmentId,
              spool_sequence: packet.sequence,
              captured_at_ms: packet.capturedAtMs,
            },
            packet.payload.length,
          ));
          socket.send(packet.payload);
        }
      };

      socket.onmessage = async (event) => {
        if (typeof event.data !== 'string') return;
        try {
          const message = JSON.parse(event.data);
          if (message.type !== 'audio-ack') return;
          const key = `${message.spool_segment_id}:${message.sequence}`;
          const packet = pending.get(key);
          if (!packet) return;
          await this.options.acknowledge(packet);
          pending.delete(key);
          if (!pending.size) {
            socket.send(wyomingHeader('audio-stop', { timestamp: Date.now() }));
            socket.close(1000, 'backlog-complete');
          }
        } catch (cause) {
          fail(cause);
          try { socket.close(1011, 'backlog-failed'); } catch {}
        }
      };

      socket.onerror = (event) => fail(new Error(`Backlog WebSocket error: ${String(event)}`));
      socket.onclose = (event) => {
        if (!pending.size) {
          if (!settled) {
            settled = true;
            resolve();
          }
          return;
        }
        fail(new Error(
          `Backlog WebSocket closed before ${pending.size} packet(s) were acknowledged`
          + (event.reason ? `: ${event.reason}` : ''),
        ));
      };
    });
  }
}
