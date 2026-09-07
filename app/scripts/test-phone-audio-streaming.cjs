const assert = require('node:assert/strict');
const Module = require('node:module');
const path = require('node:path');
const ts = require('typescript');
const fs = require('node:fs');

function loadTypeScript(sourcePath, mocks) {
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
  const originalRequire = loaded.require.bind(loaded);
  loaded.require = (request) => mocks[request] ?? originalRequire(request);
  loaded._compile(compiled.outputText, sourcePath);
  return loaded.exports;
}

const ProcessingProfile = {
  SOURCE_NATIVE: 1,
  DUPLEX_AEC: 2,
  DUPLEX_ISOLATED: 3,
  HALF_DUPLEX: 4,
};
const DeliveryClass = { LIVE: 1, RECOVERED: 2 };
const beginCaptureCalls = [];
const sentPackets = [];
const socketBearerTokens = [];
const socketFrameDurations = [];
const diagnostics = [];
let backendStops = 0;
let socketCloses = 0;

class MockAudioV2Socket {
  constructor(options) {
    this.options = options;
    this.activeBinding = null;
    socketBearerTokens.push(options.bearerToken);
    socketFrameDurations.push(options.uplinkFrameDurationMs);
  }

  async connect() {}

  async beginCapture(options) {
    beginCaptureCalls.push(options);
    this.activeBinding = {
      captureSessionId: { value: `capture-${beginCaptureCalls.length}` },
      voiceSessionId: { value: options.deliveryClass === DeliveryClass.LIVE ? 'voice-live' : '' },
      captureEpoch: options.captureEpoch,
    };
    return this.activeBinding;
  }

  sendPacket(packet) {
    sentPackets.push(packet);
    this.options.onPacketAccepted(packet.sequence);
  }

  async stopCapture() {
    backendStops += 1;
    this.activeBinding = null;
  }

  voiceReady() {}
  heartbeat() {}
  acknowledgePlayback() {}
  close() {
    socketCloses += 1;
    this.activeBinding = null;
  }
}

const noDiagnostics = new Proxy({
  frameSent: bytes => diagnostics.push(['sent', bytes]),
  packetAccepted: sequence => diagnostics.push(['accepted', sequence]),
}, { get: (target, property) => target[property] ?? (() => {}) });

const sourcePath = path.join(__dirname, '../src/hooks/useAudioStreamer.ts');
const { useAudioStreamer } = loadTypeScript(sourcePath, {
  '@bufbuild/protobuf': { create: (_schema, value) => value },
  react: {
    useCallback: callback => callback,
    useRef: value => ({ current: value }),
    useState: value => [value, () => {}],
  },
  'react-native': { Platform: { OS: 'ios' } },
  'react-native-base64': { encode: value => value },
  '../../modules/chronicle-duplex-audio': {
    addPlaybackStateListener: () => ({ remove() {} }),
    cancelResponse: async () => {},
    scheduleResponse: async () => {},
  },
  '../protocol/audioV2': {
    CaptureCapabilitiesSchema: {},
    DataPurpose: { NORMAL_CAPTURE: 1 },
    DeliveryClass,
    DeviceKind: { IOS_PHONE: 1, ANDROID_PHONE: 2, OMI: 3 },
    DuplexMode: { FULL: 1, ISOLATED: 2, HALF: 3 },
    EffectStatusSchema: {},
    InputRoute: { BUILT_IN_MIC: 1, BLUETOOTH_HFP: 2, WIRED_MIC: 3, USB: 4, REMOTE: 5 },
    OutputRoute: { SPEAKERPHONE: 1, EARPIECE: 2, HEADPHONES: 3, BLUETOOTH_HFP: 4, USB: 5, REMOTE: 6 },
    PlaybackState: { STARTED: 1, DONE: 2, CANCELLED: 3, FAILED: 4 },
    ProcessingProfile,
  },
  '../protocol/audioV2Socket': { AudioV2Socket: MockAudioV2Socket },
  '../services/auth': { getValidToken: async () => 'fresh-token' },
  '../services/phoneAudioDiagnostics': { phoneAudioDiagnostics: noDiagnostics },
});

(async () => {
  let nativeStops = 0;
  const phoneVoice = {
    captureEpoch: 7,
    capabilities: {
      mode: 'duplex_full',
      input_route: 'built_in_mic',
      output_route: 'speakerphone',
      native_sample_rate: 48_000,
      aec: { requested: true, available: true, enabled: true },
      noise_suppression: { requested: true, available: true, enabled: true },
    },
    stopCapture: async () => { nativeStops += 1; },
  };
  const streamer = useAudioStreamer();

  await streamer.startStreaming(
    'wss://chronicle.invalid/ws/audio',
    { kind: 'phone', ...phoneVoice },
  );

  assert.deepEqual(socketBearerTokens, ['fresh-token'], 'audio must use the managed token source');
  assert.deepEqual(socketFrameDurations, [20], 'phone capture must declare 20 ms Opus');
  assert.equal(beginCaptureCalls.length, 1, 'one button press must create exactly one backend capture');
  assert.equal(beginCaptureCalls[0].deliveryClass, DeliveryClass.LIVE);
  assert.equal(beginCaptureCalls[0].captureEpoch, 7);
  assert.equal(beginCaptureCalls[0].processingProfile, ProcessingProfile.DUPLEX_AEC);
  assert.equal(beginCaptureCalls[0].recoveryBatchId, undefined, 'the clean path has no recovery capture');

  const now = performance.now();
  streamer.sendFrame('phone', {
    captureEpoch: 6,
    capturedAtMs: 1_780_000_000_000,
    monotonicTimestampMs: now,
    frameDurationMs: 20,
    opus: new Uint8Array([9]),
  });
  streamer.sendFrame('phone', {
    captureEpoch: 7,
    capturedAtMs: 1_780_000_000_020,
    monotonicTimestampMs: now + 20,
    frameDurationMs: 20,
    opus: new Uint8Array([1, 2, 3]),
  });
  assert.equal(sentPackets.length, 1, 'only the active native epoch may send');
  assert.equal(sentPackets[0].sequence, 0);
  assert.equal(sentPackets[0].capturedAtMs, 1_780_000_000_020);
  assert.deepEqual(Array.from(sentPackets[0].opus), [1, 2, 3]);
  assert.deepEqual(diagnostics, [['sent', 3], ['accepted', 0]]);

  await streamer.stopStreaming();
  assert.equal(backendStops, 1, 'the capture has one stop owner');
  assert.equal(nativeStops, 1, 'stopping the stream also stops the native phone session');
  assert.equal(socketCloses, 1);

  const wearableStreamer = useAudioStreamer();
  await wearableStreamer.startStreaming(
    'wss://chronicle.invalid/ws/audio',
    { kind: 'wearable', sourceId: 'neo-1' },
  );
  assert.deepEqual(socketFrameDurations, [20, 60], 'wearable capture must declare 60 ms Opus');
  assert.equal(beginCaptureCalls.length, 2, 'wearable also uses one live capture');
  assert.equal(beginCaptureCalls[1].deliveryClass, DeliveryClass.LIVE);
  await wearableStreamer.stopStreaming();

  console.log('phone audio streaming tests passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
