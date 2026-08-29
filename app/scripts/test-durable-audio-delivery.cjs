const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const ts = require('typescript');

function loadTypeScript(sourcePath) {
  const source = fs.readFileSync(sourcePath, 'utf8');
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      esModuleInterop: true,
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
      strict: true,
    },
    fileName: sourcePath,
  });
  const loaded = new Module(sourcePath, module);
  loaded.filename = sourcePath;
  loaded.paths = Module._nodeModulePaths(path.dirname(sourcePath));
  loaded._compile(compiled.outputText, sourcePath);
  return loaded.exports;
}

const deliveryPath = path.join(__dirname, '../src/services/durableAudioDelivery.ts');
const {
  DurableAudioDeliveryCoordinator,
  DurableBacklogUploadSession,
} = loadTypeScript(deliveryPath);

const packet = (segmentId, sequence, capturedAtMs) => ({
  fileName: `${segmentId}.spool`,
  segmentId,
  sequence,
  capturedAtMs,
  payload: new Uint8Array([sequence]),
});

(async () => {
  const livePackets = [];
  const backlogPackets = [];
  let releaseBacklog;
  let markBacklogStarted;
  const backlogStarted = new Promise((resolve) => { markBacklogStarted = resolve; });
  const backlogGate = new Promise((resolve) => { releaseBacklog = resolve; });

  const coordinator = new DurableAudioDeliveryCoordinator({
    captureStartedAtMs: 2_000,
    sendLive: async (packets) => livePackets.push(...packets),
    sendBacklog: async (packets) => {
      markBacklogStarted();
      await backlogGate;
      backlogPackets.push(...packets);
    },
  });

  const recovery = coordinator.recover([packet('old', 0, 1_000)]);
  await backlogStarted;

  await coordinator.captured(packet('live', 0, 2_001));
  assert.deepEqual(
    livePackets.map(({ segmentId }) => segmentId),
    ['live'],
    'fresh capture must not wait for the historical backlog lane',
  );

  releaseBacklog();
  await recovery;
  assert.deepEqual(
    backlogPackets.map(({ segmentId }) => segmentId),
    ['old'],
    'historical packets remain assigned to the backlog lane',
  );

  class FakeSocket {
    constructor() {
      this.readyState = 0;
      this.sent = [];
      this.closed = null;
    }

    send(value) {
      this.sent.push(value);
    }

    close(code, reason) {
      this.readyState = 3;
      this.closed = { code, reason };
      this.onclose?.({ code, reason });
    }

    open() {
      this.readyState = 1;
      return this.onopen?.();
    }

    message(value) {
      return this.onmessage?.({ data: JSON.stringify(value) });
    }
  }

  const socket = new FakeSocket();
  const retired = [];
  const uploader = new DurableBacklogUploadSession({
    url: 'wss://chronicle.test/ws?codec=opus',
    audioFormat: { rate: 16_000, width: 2, channels: 1 },
    createSocket: () => socket,
    acknowledge: async (acknowledgedPacket) => {
      retired.push(`${acknowledgedPacket.segmentId}:${acknowledgedPacket.sequence}`);
    },
  });
  const oldPackets = [packet('old-a', 0, 1_000), packet('old-b', 4, 1_500)];
  const upload = uploader.upload(oldPackets);
  await socket.open();

  const sentHeaders = socket.sent.filter((value) => typeof value === 'string').map(JSON.parse);
  assert.equal(sentHeaders[0].type, 'audio-start');
  assert.deepEqual(
    sentHeaders.slice(1).map(({ type, data }) => [
      type,
      data.spool_segment_id,
      data.spool_sequence,
      data.captured_at_ms,
    ]),
    [
      ['audio-chunk', 'old-a', 0, 1_000],
      ['audio-chunk', 'old-b', 4, 1_500],
    ],
    'backlog session must preserve durable identity and capture time',
  );

  await socket.message({ type: 'audio-ack', spool_segment_id: 'old-b', sequence: 4 });
  assert.equal(socket.closed, null, 'one ACK cannot close a session with another packet pending');
  await socket.message({ type: 'audio-ack', spool_segment_id: 'old-a', sequence: 0 });
  await upload;

  assert.deepEqual(retired, ['old-b:4', 'old-a:0']);
  assert.equal(JSON.parse(socket.sent.at(-1)).type, 'audio-stop');
  assert.deepEqual(socket.closed, { code: 1000, reason: 'backlog-complete' });

  const stopLivePackets = [];
  const stopBacklogPackets = [];
  const stopCoordinator = new DurableAudioDeliveryCoordinator({
    captureStartedAtMs: 3_000,
    sendLive: async (packets) => stopLivePackets.push(...packets),
    sendBacklog: async (packets) => stopBacklogPackets.push(...packets),
  });
  const unacknowledgedCurrentPacket = packet('current-unacked', 2, 3_100);
  await stopCoordinator.captured(unacknowledgedCurrentPacket);
  await stopCoordinator.finishCapture([unacknowledgedCurrentPacket]);
  assert.deepEqual(
    stopLivePackets.map(({ segmentId }) => segmentId),
    ['current-unacked'],
  );
  assert.deepEqual(
    stopBacklogPackets.map(({ segmentId }) => segmentId),
    ['current-unacked'],
    'stopping capture must flush every durable packet still awaiting an ACK',
  );

  console.log('durable audio delivery contract tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
