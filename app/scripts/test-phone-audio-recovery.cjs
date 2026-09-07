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
let pendingReads = 0;

class MockAudioV2Socket {
  constructor(options) {
    this.options = options;
    this.activeBinding = null;
  }

  async connect() {}

  async beginCapture(options) {
    beginCaptureCalls.push(options);
    this.activeBinding = {
      captureSessionId: { value: `capture-${beginCaptureCalls.length}` },
      voiceSessionId: { value: beginCaptureCalls.length === 2 ? 'voice-live' : '' },
      captureEpoch: options.captureEpoch,
    };
    return this.activeBinding;
  }

  sendPacket(packet) {
    this.options.onPacketAccepted(packet.sequence);
  }

  async stopCapture() {
    this.activeBinding = null;
  }

  voiceReady() {}
  heartbeat() {}
  close() {}
  acknowledgePlayback() {}
}

const noDiagnostics = new Proxy({}, { get: () => () => {} });
const sourcePath = path.join(__dirname, '../src/hooks/useAudioStreamer.ts');
const { useAudioStreamer } = loadTypeScript(sourcePath, {
  '@bufbuild/protobuf': { create: (_schema, value) => value },
  '@react-native-community/netinfo': { addEventListener: () => () => {} },
  react: {
    useCallback: (callback) => callback,
    useEffect: () => {},
    useRef: (value) => ({ current: value }),
    useState: (value) => [value, () => {}],
  },
  'react-native': { Platform: { OS: 'ios' } },
  'react-native-base64': { encode: (value) => value },
  '../../modules/chronicle-duplex-audio': {
    addPlaybackStateListener: () => ({ remove() {} }),
    addRouteChangeListener: () => ({ remove() {} }),
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
  '../services/auth': { refreshToken: async () => 'refreshed-token' },
  '../services/durableAudioSpool': {
    durableAudioSpool: {
      async pendingPackets() {
        pendingReads += 1;
        return pendingReads === 1
          ? [{
            fileName: 'old.spool',
            segmentId: 'old',
            sequence: 7,
            capturedAtMs: 1_780_000_000_000,
            payload: new Uint8Array([1, 2, 3]),
          }]
          : [];
      },
      async acknowledge() {},
      close() {},
      append() {
        throw new Error('not used by this test');
      },
    },
  },
  '../services/phoneAudioDiagnostics': { phoneAudioDiagnostics: noDiagnostics },
});

(async () => {
  const streamer = useAudioStreamer({ autoReconnectEnabled: false });
  const phoneVoice = {
    captureEpoch: 1,
    capabilities: {
      mode: 'duplex_full',
      input_route: 'built_in_mic',
      output_route: 'speakerphone',
      native_sample_rate: 48_000,
      aec: { requested: true, available: true, enabled: true },
      noise_suppression: { requested: true, available: true, enabled: true },
    },
    restartCapture: async () => phoneVoice,
    stopCapture: async () => {},
  };

  await streamer.startStreaming(
    'https://chronicle.invalid/ws/audio?token=test-token&device_name=phone-mic',
    { phoneVoice },
  );

  assert.equal(beginCaptureCalls.length, 2, 'queued audio must recover before live capture starts');
  assert.deepEqual(
    {
      captureEpoch: beginCaptureCalls[0].captureEpoch,
      processingProfile: beginCaptureCalls[0].processingProfile,
      deliveryClass: beginCaptureCalls[0].deliveryClass,
    },
    {
      captureEpoch: 0,
      processingProfile: ProcessingProfile.SOURCE_NATIVE,
      deliveryClass: DeliveryClass.RECOVERED,
    },
    'recovered source-native audio must use epoch zero',
  );
  assert.deepEqual(
    {
      captureEpoch: beginCaptureCalls[1].captureEpoch,
      processingProfile: beginCaptureCalls[1].processingProfile,
      deliveryClass: beginCaptureCalls[1].deliveryClass,
    },
    {
      captureEpoch: 1,
      processingProfile: ProcessingProfile.DUPLEX_AEC,
      deliveryClass: DeliveryClass.LIVE,
    },
    'the following live duplex capture must retain the native phone epoch',
  );

  await streamer.stopStreaming();
  console.log('phone audio recovery tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
